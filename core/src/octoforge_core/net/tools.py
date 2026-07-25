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
from octoforge_core.net.external import ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
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
TRUNCATED_SUFFIX = "\n...[truncated]"
DEFAULT_TIMEOUT_SECONDS = 30.0

CALL_NAME = "external_call"
CALL_DESCRIPTION = (
    "Execute an external call described by an endpoint record from the store. "
    "Skills name the endpoints they use; before the FIRST call of an endpoint in "
    "this process, resolve its contract with endpoint_get and pass exactly the "
    "params it declares — never guess them. When no skill names an endpoint, "
    "discover candidates with recall(type=endpoint, ...)."
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
    """HTTP methods allowed for the tool."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


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
            "additionalProperties": {"type": "string"},
            "description": "Parameters declared by the endpoint's params_schema",
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
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        guard: SsrfGuard,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http_client
        self._guard = guard
        self._timeout = timeout_seconds

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
        await self._guard.check(params.url)
        response = await self._http.request(
            params.method.value,
            params.url,
            headers=params.headers,
            content=params.body,
            follow_redirects=False,
            timeout=self._timeout,
        )
        body = response.text
        if len(body) > MAX_RESPONSE_CHARS:
            body = body[:MAX_RESPONSE_CHARS] + TRUNCATED_SUFFIX
        return f"HTTP {response.status_code}\n{body}"


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
        return f"HTTP {result.status}\n{result.body}"


def _parse_params(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        return dict(raw)
    raise ToolArgumentsError("params must be an object of strings")
