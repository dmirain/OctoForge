"""Agent-facing registration of external MCP servers."""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from octoforge_core.mcp.api import McpError, McpServer, McpServerNameTakenError, McpServerStore
from octoforge_core.mcp.sync import McpToolSync
from octoforge_core.mcp.tool_args import parse_server
from octoforge_core.mcp.tool_contract import DESCRIPTION, NAME, SCHEMA, SYNC_FAILED_TEMPLATE
from octoforge_core.mcp.tool_report import ready_report, with_secret_hint
from octoforge_core.net.errors import SsrfBlockedError
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.secrets.api import SecretNotFoundError, SecretStore
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec


@dataclass(frozen=True, slots=True)
class McpAddServices:
    store: McpServerStore
    sync: McpToolSync
    guard: SsrfGuard
    secrets: SecretStore | None = None


class McpAddTool:
    """Register one MCP server and run its first mirror sync."""

    def __init__(self, services: McpAddServices) -> None:
        self._services = services

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=NAME, description=DESCRIPTION, parameters_schema=SCHEMA)

    def visible_to(self, context: ToolContext) -> bool:
        return feature_enabled(context.enabled_features, FeatureCode.MCP_ADD)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.MCP_ADD)
        server = parse_server(arguments)
        try:
            await self._services.guard.check(server.url)
        except SsrfBlockedError as exc:
            return f"error: {exc}"
        try:
            added = await self._services.store.add(server, context.user_id)
        except McpServerNameTakenError as exc:
            return f"error: server name {exc} already points at a different URL; pass another name"
        secret_value = await self._callers_secret(added, context.user_id)
        try:
            outcome = await self._services.sync.sync_server(added, secret_value)
        except McpError as exc:
            report = SYNC_FAILED_TEMPLATE.format(name=added.name, error=exc)
            return with_secret_hint(added, report)
        return ready_report(added, outcome)

    async def _callers_secret(self, server: McpServer, user_id: str) -> str | None:
        if server.auth_secret_code is None or self._services.secrets is None:
            return None
        host = (urlsplit(server.url).hostname or "").lower()
        try:
            resolved = await self._services.secrets.resolve(
                user_id,
                server.auth_secret_code,
                host,
            )
        except SecretNotFoundError:
            return None
        return resolved.value
