"""Basic skill performing HTTP requests."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

import httpx

from octoforge_core.net.guard import SsrfGuard
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "http_request"
SKILL_DESCRIPTION = (
    "Perform an HTTP request and return the response status and body. "
    "Use it to call external APIs and fetch web pages."
)
MAX_RESPONSE_CHARS = 4000
TRUNCATED_SUFFIX = "\n...[truncated]"
DEFAULT_TIMEOUT_SECONDS = 30.0


class HttpMethod(StrEnum):
    """HTTP methods allowed for the skill."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


PARAMETERS_SCHEMA: dict[str, Any] = {
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
            raise SkillArgumentsError("url must be a non-empty string")
        headers = cls._parse_headers(arguments.get("headers"))
        body = arguments.get("body")
        if body is not None and not isinstance(body, str):
            raise SkillArgumentsError("body must be a string")
        return cls(method=method, url=url, headers=headers, body=body)

    @staticmethod
    def _parse_method(raw: object) -> HttpMethod:
        try:
            return HttpMethod(str(raw))
        except ValueError as exc:
            raise SkillArgumentsError(f"unsupported method: {raw!r}") from exc

    @staticmethod
    def _parse_headers(raw: object) -> dict[str, str]:
        if raw is None:
            return {}
        if isinstance(raw, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            return dict(raw)
        raise SkillArgumentsError("headers must be an object of strings")


class HttpRequestSkill:
    """Skill that performs HTTP requests via an injected client.

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
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
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
