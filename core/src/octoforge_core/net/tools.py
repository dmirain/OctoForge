"""Outbound HTTP tools: one-off http_request and endpoint-driven external_call."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

import httpx

from octoforge_core.net.external import ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

REQUEST_NAME = "http_request"
REQUEST_DESCRIPTION = (
    "Perform an HTTP request and return the response status and body. "
    "Use it to call external APIs and fetch web pages."
)
MAX_RESPONSE_CHARS = 4000
TRUNCATED_SUFFIX = "\n...[truncated]"
DEFAULT_TIMEOUT_SECONDS = 30.0

CALL_NAME = "external_call"
CALL_DESCRIPTION = (
    "Execute an external call described by an endpoint instruction from the store. "
    "Use skills_search to discover available endpoints, then call them by name "
    "with the params declared in the endpoint record."
)


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
