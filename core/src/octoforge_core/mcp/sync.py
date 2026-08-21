"""Mirror MCP tool lists into searchable endpoint records."""

import logging
from dataclasses import dataclass

from octoforge_core.instructions.api import InstructionService
from octoforge_core.mcp.api import McpClient, McpError, McpServer, McpServerStore, McpToolDescriptor
from octoforge_core.mcp.mirror import (
    DESCRIPTION_MAX_CHARS,
    auth_headers,
    mirror_content,
    mirror_title,
    write_mirror,
)
from octoforge_core.mcp.skill_refresh import McpSkillRefresher
from octoforge_core.mcp.skills import McpSkillGenerator, SkillPattern
from octoforge_core.mcp.sync_auth import subscriber_secret
from octoforge_core.mcp.sync_loop import McpSyncLoop
from octoforge_core.secrets.api import SecretStore

logger = logging.getLogger(__name__)

MAX_TOOLS_PER_SERVER = 100
TOO_MANY_TOOLS_TEMPLATE = "server lists {count} tools; the cap is {cap} - refusing to mirror it"
DEFAULT_SYNC_INTERVAL_SECONDS = 3600.0

__all__ = [
    "DEFAULT_SYNC_INTERVAL_SECONDS",
    "DESCRIPTION_MAX_CHARS",
    "MAX_TOOLS_PER_SERVER",
    "McpSyncLoop",
    "McpSyncOptions",
    "McpSyncServices",
    "McpToolSync",
    "SyncOutcome",
    "mirror_content",
    "mirror_title",
]


@dataclass(frozen=True, slots=True)
class McpSyncServices:
    store: McpServerStore
    client: McpClient
    instructions: InstructionService


@dataclass(frozen=True, slots=True)
class McpSyncOptions:
    secrets: SecretStore | None = None
    skills: McpSkillGenerator | None = None


DEFAULT_SYNC_OPTIONS = McpSyncOptions()


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    tool_names: tuple[str, ...]
    updated: int
    removed: int
    skill: SkillPattern | None

    @property
    def tools(self) -> int:
        return len(self.tool_names)


class McpToolSync:
    """Diff one server's tool list and persist only the changed mirror records."""

    def __init__(
        self,
        services: McpSyncServices,
        options: McpSyncOptions = DEFAULT_SYNC_OPTIONS,
    ) -> None:
        self._store = services.store
        self._client = services.client
        self._instructions = services.instructions
        self._secrets = options.secrets
        self._skill_refresher = McpSkillRefresher(services.store, options.skills)

    async def sync_server(self, server: McpServer, secret_value: str | None = None) -> SyncOutcome:
        tools = await self._list_tools(server, secret_value)
        update = await write_mirror(self._instructions, server, tools)
        await self._store.mark_synced(server.id, None)
        return SyncOutcome(
            tuple(tool.name for tool in tools),
            update.updated,
            update.removed,
            await self._skill_refresher.refresh(server, tools),
        )

    async def _list_tools(
        self,
        server: McpServer,
        secret_value: str | None,
    ) -> list[McpToolDescriptor]:
        try:
            tools = await self._client.list_tools(server.url, auth_headers(server, secret_value))
            if len(tools) > MAX_TOOLS_PER_SERVER:
                raise McpError(
                    TOO_MANY_TOOLS_TEMPLATE.format(count=len(tools), cap=MAX_TOOLS_PER_SERVER)
                )
            return tools
        except McpError as exc:
            await self._store.mark_synced(server.id, str(exc))
            raise

    async def sync_all(self) -> None:
        for server in await self._store.list_all():
            try:
                secret = await subscriber_secret(self._store, self._secrets, server)
                await self.sync_server(server, secret)
            except McpError as exc:
                logger.warning("MCP sync failed: server=%s error=%s", server.name, exc)
            except Exception:
                logger.exception("MCP sync crashed: server=%s", server.name)
