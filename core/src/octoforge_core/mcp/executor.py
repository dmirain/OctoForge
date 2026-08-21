"""Validate, execute and scrub mirrored MCP tool calls behind external_call."""

from urllib.parse import urlsplit

from octoforge_core.mcp.api import McpClient, McpError, McpServer, McpServerStore, parse_mirror
from octoforge_core.mcp.call import McpInvocation, invoke
from octoforge_core.mcp.executor_types import (
    DEFAULT_EXECUTOR_OPTIONS,
    SERVER_GONE_TEMPLATE,
    TOOL_ERROR_TEMPLATE,
    McpExecutorOptions,
    McpSpillRequest,
)
from octoforge_core.mcp.executor_values import scrub, truncate, validate_arguments
from octoforge_core.net.collections.ingest import SpillRequest
from octoforge_core.net.errors import ExternalCallError
from octoforge_core.net.external import (
    SECRET_MISSING_TEMPLATE,
    SECRETS_DISABLED_MESSAGE,
    ExternalCallResult,
    KindCallRequest,
)
from octoforge_core.secrets.api import (
    ResolvedSecret,
    SecretHostMismatchError,
    SecretNotFoundError,
)


class McpMirrorCallExecutor:
    def __init__(
        self,
        store: McpServerStore,
        client: McpClient,
        options: McpExecutorOptions = DEFAULT_EXECUTOR_OPTIONS,
    ) -> None:
        self._store = store
        self._client = client
        self._options = options

    async def execute(self, request: KindCallRequest) -> ExternalCallResult:
        try:
            mirror = parse_mirror(request.content)
        except McpError as exc:
            raise ExternalCallError(str(exc)) from exc
        server = await self._store.get_by_name(mirror.server)
        if server is None:
            raise ExternalCallError(SERVER_GONE_TEMPLATE.format(server=mirror.server))
        validate_arguments(mirror.input_schema, request.params, request.content)
        secret = await self._resolve_secret(server, request.user_id)
        headers = (
            {server.auth_header: server.auth_format.format(value=secret.value)}
            if secret is not None
            else {}
        )
        result = await invoke(
            self._client,
            server,
            McpInvocation(headers, mirror.tool, request.params, request.content),
        )
        scrubbed = scrub(result.text, secret)
        body = await self._spill(
            McpSpillRequest(
                scrubbed,
                request.user_id,
                request.scope,
                f"mcp:{mirror.server}/{mirror.tool}",
            )
        )
        rendered = body or truncate(scrubbed, self._options.truncate_chars)
        if result.is_error:
            rendered = TOOL_ERROR_TEMPLATE.format(text=rendered, contract=request.content)
        return ExternalCallResult(status=0, body=rendered)

    async def _resolve_secret(
        self,
        server: McpServer,
        user_id: str | None,
    ) -> ResolvedSecret | None:
        if server.auth_secret_code is None:
            return None
        if self._options.secrets is None:
            raise ExternalCallError(SECRETS_DISABLED_MESSAGE)
        if user_id is None:
            raise ExternalCallError(
                "this MCP server requires a per-user secret: no user in context"
            )
        host = (urlsplit(server.url).hostname or "").lower()
        try:
            return await self._options.secrets.resolve(user_id, server.auth_secret_code, host)
        except SecretNotFoundError:
            raise ExternalCallError(
                SECRET_MISSING_TEMPLATE.format(code=server.auth_secret_code, host=host)
            ) from None
        except SecretHostMismatchError as exc:
            raise ExternalCallError(str(exc)) from None

    async def _spill(self, request: McpSpillRequest) -> str | None:
        if self._options.spill is None or request.user_id is None:
            return None
        return await self._options.spill.spill(
            SpillRequest(
                owner_id=request.user_id,
                body=request.body,
                content_type="",
                source=request.source,
                wire_truncated=False,
                scope=request.scope,
            )
        )
