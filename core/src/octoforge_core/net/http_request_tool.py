"""Guarded fallback HTTP request tool."""

from typing import Any
from urllib.parse import urlsplit

import httpx

from octoforge_core.net.collections.ingest import SpillRequest
from octoforge_core.net.errors import EgressBlockedError
from octoforge_core.net.external import read_capped_text
from octoforge_core.net.guard import SsrfGuard, matches_url_prefix
from octoforge_core.net.http_request_types import (
    DEFAULT_HTTP_TOOL_CONFIG,
    HttpRequestParams,
    HttpRequestToolConfig,
)
from octoforge_core.net.tool_contract import (
    REQUEST_DESCRIPTION,
    REQUEST_NAME,
    REQUEST_SCHEMA,
)
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
BODY_TOO_LARGE_SUFFIX = "\n...[response exceeded the size limit and was cut]"
TRUNCATED_SUFFIX = "\n...[truncated]"
EGRESS_BLOCKED_TEMPLATE = (
    "this installation only allows http_request to: {allowed}. "
    "For anything else, look for a stored endpoint with recall(type=endpoint)."
)


class HttpRequestTool:
    """Perform one guarded request to an optional origin allowlist."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        guard: SsrfGuard,
        config: HttpRequestToolConfig = DEFAULT_HTTP_TOOL_CONFIG,
    ) -> None:
        self._http = http_client
        self._guard = guard
        self._config = config

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(REQUEST_NAME, REQUEST_DESCRIPTION, REQUEST_SCHEMA)

    def visible_to(self, context: ToolContext) -> bool:
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        params = HttpRequestParams.from_arguments(arguments)
        self._check_allowed(params.url)
        await self._guard.check(params.url)
        async with self._http.stream(
            params.method.value,
            params.url,
            headers=params.headers,
            content=params.body,
            follow_redirects=False,
            timeout=self._config.timeout_seconds,
        ) as response:
            body, truncated = await read_capped_text(
                response,
                self._config.spill.wire_limit_bytes
                if self._config.spill is not None
                else MAX_RESPONSE_BYTES,
            )
            content_type = response.headers.get("content-type", "")
        if self._config.spill is not None:
            passport = await self._config.spill.spill(
                SpillRequest(
                    owner_id=context.user_id,
                    body=body,
                    content_type=content_type,
                    source=f"http:{urlsplit(params.url).hostname or ''}",
                    wire_truncated=truncated,
                    scope=context.owner_task_id or "",
                )
            )
            if passport is not None:
                return f"HTTP {response.status_code}\n{passport}"
        if len(body) > self._config.max_chars:
            body = body[: self._config.max_chars] + TRUNCATED_SUFFIX
        elif truncated:
            body += BODY_TOO_LARGE_SUFFIX
        return f"HTTP {response.status_code}\n{body}"

    def _check_allowed(self, url: str) -> None:
        allowed = self._config.allowed_origins
        if allowed and not any(matches_url_prefix(url, origin) for origin in allowed):
            raise EgressBlockedError(EGRESS_BLOCKED_TEMPLATE.format(allowed=", ".join(allowed)))
