"""Outbound HTTP tools: http_request, endpoint_get and endpoint-driven external_call."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

import httpx

from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
)
from octoforge_core.net.errors import EgressBlockedError
from octoforge_core.net.external import ExternalCallExecutor, read_capped_text
from octoforge_core.net.guard import SsrfGuard, matches_url_prefix
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

REQUEST_NAME = "http_request"
REQUEST_DESCRIPTION = (
    "Perform an HTTP request and return the response status and body. "
    "The fallback path, not the default one: a stored endpoint record executed by "
    "external_call carries the checked URL, params and auth, so run recall "
    "first and use this tool only when no endpoint covers the call. Never explore an "
    "API with it — do not guess URLs or parameters, and do not repeat a failed request "
    "with variations; report the failure instead."
)
MAX_RESPONSE_CHARS = 4000
# Hard byte ceiling read from the wire. The character cap above trims what the
# model sees; this one decides how much is ever held in memory, because a URL
# the agent was told to fetch can answer with gigabytes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
BODY_TOO_LARGE_SUFFIX = "\n...[response exceeded the size limit and was cut]"
EGRESS_BLOCKED_TEMPLATE = (
    "this installation only allows http_request to: {allowed}. "
    "For anything else, look for a stored endpoint with recall(type=endpoint)."
)
TRUNCATED_SUFFIX = "\n...[truncated]"
DEFAULT_TIMEOUT_SECONDS = 30.0

CALL_NAME = "external_call"
CALL_DESCRIPTION = (
    "Execute an external call described by an endpoint record from the store. "
    "Skills name the endpoints they use; before the FIRST call of an endpoint in "
    "this process, resolve its contract with endpoint_get and pass exactly the "
    "params it declares — never guess them. When no skill names an endpoint, "
    "discover candidates with recall(type=endpoint, ...). Records under the "
    "mcp/ namespace are MCP tools: their contract is a JSON Schema and params "
    "may carry structured values (objects, arrays, numbers) exactly as the "
    "schema declares."
)

ENDPOINT_GET_NAME = "endpoint_get"
ENDPOINT_GET_DESCRIPTION = (
    "Resolve an endpoint by its EXACT name and return its contract: method, URL "
    "template and the declared params. This is the step between a skill that names "
    "an endpoint and the external_call that executes it — call it before the first "
    "external_call of an endpoint in this process instead of guessing parameters; "
    "the contract stays in context for repeat calls. Not a search: an unknown name "
    "answers not-found — discover endpoints with recall(type=endpoint, ...)."
)
ENDPOINT_NOT_FOUND_TEMPLATE = (
    "endpoint '{name}' not found; discover endpoints with recall(type=endpoint, query=...)"
)
ENDPOINT_TEMPLATE = "[endpoint] {title}\n   tags: {tags}\n{content}"
ENDPOINT_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Exact title of the endpoint record"},
    },
    "required": ["name"],
}


class HttpMethod(StrEnum):
    """HTTP methods allowed for the tool.

    Includes the WebDAV verbs (CalDAV/CardDAV need PROPFIND, REPORT,
    MKCALENDAR): the SSRF guard checks the address, not the method, and
    write verbs were always allowed, so they add no new risk class.
    LOCK/UNLOCK stay out — an agent cannot manage a lock's lifetime.
    """

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    PROPFIND = "PROPFIND"
    PROPPATCH = "PROPPATCH"
    REPORT = "REPORT"
    MKCOL = "MKCOL"
    MKCALENDAR = "MKCALENDAR"
    COPY = "COPY"
    MOVE = "MOVE"


REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {
            "type": "string",
            "enum": [method.value for method in HttpMethod],
            "description": "HTTP method",
        },
        "url": {"type": "string", "description": "Full URL including scheme"},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "Optional HTTP headers",
        },
        "body": {"type": "string", "description": "Optional request body"},
    },
    "required": ["method", "url"],
}

CALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Title of the endpoint instruction to execute"},
        "params": {
            "type": "object",
            "description": (
                "Parameters declared by the endpoint's contract: string values "
                "for classic endpoints, schema-shaped values for mcp/ records"
            ),
        },
    },
    "required": ["name"],
}


@dataclass(frozen=True, slots=True)
class HttpRequestParams:
    """Validated parameters of an HTTP request."""

    method: HttpMethod
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    @classmethod
    def from_arguments(cls, arguments: dict[str, Any]) -> Self:
        """Validate raw LLM arguments into typed params."""
        method = cls._parse_method(arguments.get("method"))
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            raise ToolArgumentsError("url must be a non-empty string")
        headers = cls._parse_headers(arguments.get("headers"))
        body = arguments.get("body")
        if body is not None and not isinstance(body, str):
            raise ToolArgumentsError("body must be a string")
        return cls(method=method, url=url, headers=headers, body=body)

    @staticmethod
    def _parse_method(raw: object) -> HttpMethod:
        try:
            return HttpMethod(str(raw))
        except ValueError as exc:
            raise ToolArgumentsError(f"unsupported method: {raw!r}") from exc

    @staticmethod
    def _parse_headers(raw: object) -> dict[str, str]:
        if raw is None:
            return {}
        if isinstance(raw, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            return dict(raw)
        raise ToolArgumentsError("headers must be an object of strings")


class HttpRequestTool:
    """Tool that performs HTTP requests via an injected client.

    Redirects are not followed: the SSRF guard validates only the requested
    URL, and following a redirect would re-enter unchecked address space.
    (This matches the previous behavior — httpx never followed redirects here
    by default — now made explicit.)

    `allowed_origins` confines the tool to named destinations. The guard blocks
    private address space, which stops an agent from reaching the intranet, but
    not from posting a dialog's contents to a public address somebody put in a
    prompt-injected page. An installation that only ever calls known services
    closes that channel by listing them; an empty tuple keeps the tool open,
    which is the default because a general assistant needs the open web.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        guard: SsrfGuard,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self._http = http_client
        self._guard = guard
        self._timeout = timeout_seconds
        self._allowed_origins = allowed_origins

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=REQUEST_NAME,
            description=REQUEST_DESCRIPTION,
            parameters_schema=REQUEST_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, perform the request and format the response."""
        params = HttpRequestParams.from_arguments(arguments)
        self._check_allowed(params.url)
        await self._guard.check(params.url)
        async with self._http.stream(
            params.method.value,
            params.url,
            headers=params.headers,
            content=params.body,
            follow_redirects=False,
            timeout=self._timeout,
        ) as response:
            body, truncated = await read_capped_text(response, MAX_RESPONSE_BYTES)
        if len(body) > MAX_RESPONSE_CHARS:
            body = body[:MAX_RESPONSE_CHARS] + TRUNCATED_SUFFIX
        elif truncated:
            body += BODY_TOO_LARGE_SUFFIX
        return f"HTTP {response.status_code}\n{body}"

    def _check_allowed(self, url: str) -> None:
        """Refuse an origin the installation did not list (no list = anywhere)."""
        if not self._allowed_origins:
            return
        if any(matches_url_prefix(url, origin) for origin in self._allowed_origins):
            return
        raise EgressBlockedError(
            EGRESS_BLOCKED_TEMPLATE.format(allowed=", ".join(self._allowed_origins))
        )


class EndpointGetTool:
    """Late binding of endpoints: exact-name lookup of the call contract.

    Skills reference endpoints by name; this tool turns the name into the
    contract the model needs to construct a correct external_call. A lookup,
    not a search — discovery is recall's job (type=endpoint).
    """

    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=ENDPOINT_GET_NAME,
            description=ENDPOINT_GET_DESCRIPTION,
            parameters_schema=ENDPOINT_GET_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Resolve the endpoint visible to the caller; not-found is text, not an error."""
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolArgumentsError("name must be a non-empty string")
        try:
            record = await self._service.get_by_name(
                name, InstructionType.ENDPOINT, user_id=context.user_id
            )
        except InstructionNotFoundError:
            return ENDPOINT_NOT_FOUND_TEMPLATE.format(name=name)
        return ENDPOINT_TEMPLATE.format(
            title=record.title,
            tags=", ".join(record.tags) if record.tags else "-",
            content=record.content,
        )


class ExternalCallTool:
    """Thin adapter over the ExternalCallExecutor."""

    def __init__(self, executor: ExternalCallExecutor) -> None:
        self._executor = executor

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=CALL_NAME,
            description=CALL_DESCRIPTION,
            parameters_schema=CALL_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, run the call and format status + body."""
        name = arguments.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ToolArgumentsError("name must be a non-empty string")
        params = _parse_params(arguments.get("params"))
        result = await self._executor.execute(name, params, user_id=context.user_id)
        # status 0: the transport carried no HTTP status (a kind delegate)
        return f"HTTP {result.status}\n{result.body}" if result.status else result.body


def _parse_params(raw: object) -> dict[str, Any]:
    """Accept any object: value shapes belong to the record's own contract.

    A classic endpoint rejects non-string values with its contract attached
    (executor-side, where the contract is at hand); an MCP mirror takes the
    structured values its schema declares.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict) and all(isinstance(key, str) for key in raw):
        return dict(raw)
    raise ToolArgumentsError("params must be an object")
