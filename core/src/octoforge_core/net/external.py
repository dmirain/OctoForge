"""Executor of external calls described by endpoint instruction records.

Core-side execution: the instructions module only stores/searches/ranks
endpoint records; this executor reads them through the `InstructionService`
facade, validates and renders the URL, applies the SSRF guard and the
composition-root auth whitelist, and performs the HTTP request.
"""

from dataclasses import dataclass
from urllib.parse import quote

import httpx

from octoforge_core.config import DEFAULT_TIMEOUT_SECONDS
from octoforge_core.instructions.api import InstructionService, InstructionType
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.guard import SsrfGuard, matches_url_prefix
from octoforge_core.net.tool_spec import ToolSpec, parse_tool_spec

MAX_BODY_CHARS = 8000
TRUNCATED_SUFFIX = "\n...[truncated]"
USER_ID_PLACEHOLDER = "{user_id}"


@dataclass(frozen=True, slots=True)
class ExternalCallAuth:
    """Internal authorization injected for an allowlisted origin.

    The base-url prefix is compared by parsed origin (scheme/host/port, the
    path ignored), so userinfo-spoofed URLs never receive the header.
    `header_value` may contain the `{user_id}` placeholder, substituted with
    the calling user's id at execution time; a call without a user id then
    sends no header at all.
    """

    base_url_prefix: str
    header_name: str
    header_value: str


@dataclass(frozen=True, slots=True)
class ExternalCallResult:
    """Outcome of an external call; error statuses are data, not exceptions."""

    status: int
    body: str


class ExternalCallExecutor:
    """Executes endpoint records fetched through the instructions facade."""

    def __init__(
        self,
        service: InstructionService,
        http_client: httpx.AsyncClient,
        guard: SsrfGuard,
        auth_whitelist: tuple[ExternalCallAuth, ...] = (),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._service = service
        self._http = http_client
        self._guard = guard
        self._auth_whitelist = auth_whitelist
        self._timeout = timeout_seconds

    async def execute(
        self,
        name: str,
        params: dict[str, str],
        user_id: str | None = None,
    ) -> ExternalCallResult:
        """Run the endpoint call `name` with validated params and return status + body."""
        instruction = await self._service.get_by_name(name, InstructionType.ENDPOINT)
        spec = parse_tool_spec(instruction.content)
        validated = _validate_params(spec, params)
        url = _render_url(spec, validated)
        await self._guard.check(url)
        headers = self._auth_headers_for(url, user_id)
        try:
            response = await self._http.request(
                spec.method,
                url,
                headers=headers,
                follow_redirects=False,  # a redirect would bypass the guard's URL check
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ExternalCallError(f"external call failed: {exc}") from exc
        return ExternalCallResult(status=response.status_code, body=_truncate(response.text))

    def _auth_headers_for(self, url: str, user_id: str | None) -> dict[str, str]:
        for entry in self._auth_whitelist:
            if matches_url_prefix(url, entry.base_url_prefix):
                return _render_auth_header(entry, user_id)
        return {}


def _render_auth_header(entry: ExternalCallAuth, user_id: str | None) -> dict[str, str]:
    if USER_ID_PLACEHOLDER not in entry.header_value:
        return {entry.header_name: entry.header_value}
    if user_id is None:
        return {}
    return {entry.header_name: entry.header_value.replace(USER_ID_PLACEHOLDER, user_id)}


def _validate_params(spec: ToolSpec, params: dict[str, str]) -> dict[str, str]:
    unknown = sorted(set(params) - set(spec.params))
    if unknown:
        raise ExternalCallError(f"unknown params: {', '.join(unknown)}")
    missing = sorted(
        name for name, param in spec.params.items() if param.required and name not in params
    )
    if missing:
        raise ExternalCallError(f"missing required params: {', '.join(missing)}")
    return dict(params)


def _render_url(spec: ToolSpec, params: dict[str, str]) -> str:
    quoted = {name: quote(value, safe="") for name, value in params.items()}
    return spec.url_template.format(**quoted)


def _truncate(body: str) -> str:
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[:MAX_BODY_CHARS] + TRUNCATED_SUFFIX
