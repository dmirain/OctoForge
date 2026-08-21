"""Configuration and validated arguments for the fallback HTTP tool."""

from dataclasses import dataclass, field
from typing import Any, Self

from octoforge_core.net.collections.ingest import ResponseSpill
from octoforge_core.net.tool_contract import HttpMethod
from octoforge_core.tools.errors import ToolArgumentsError

MAX_RESPONSE_CHARS = 4000
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class HttpRequestToolConfig:
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    allowed_origins: tuple[str, ...] = ()
    spill: ResponseSpill | None = None
    max_chars: int = MAX_RESPONSE_CHARS


DEFAULT_HTTP_TOOL_CONFIG = HttpRequestToolConfig()


@dataclass(frozen=True, slots=True)
class HttpRequestParams:
    method: HttpMethod
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    @classmethod
    def from_arguments(cls, arguments: dict[str, Any]) -> Self:
        try:
            method = HttpMethod(str(arguments.get("method")))
        except ValueError as exc:
            raise ToolArgumentsError(f"unsupported method: {arguments.get('method')!r}") from exc
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            raise ToolArgumentsError("url must be a non-empty string")
        headers = parse_headers(arguments.get("headers"))
        body = arguments.get("body")
        if body is not None and not isinstance(body, str):
            raise ToolArgumentsError("body must be a string")
        return cls(method, url, headers, body)


def parse_headers(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        return dict(raw)
    raise ToolArgumentsError("headers must be an object of strings")
