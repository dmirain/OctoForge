"""Fingerprint-gated refresh of a generated MCP usage skill."""

import logging

from octoforge_core.mcp.api import McpError, McpServer, McpServerStore, McpToolDescriptor
from octoforge_core.mcp.mirror import tools_fingerprint
from octoforge_core.mcp.skills import McpSkillGenerator, SkillPattern

logger = logging.getLogger(__name__)


class McpSkillRefresher:
    """Generate a usage skill only when a server's tool fingerprint changes."""

    def __init__(self, store: McpServerStore, generator: McpSkillGenerator | None) -> None:
        self._store = store
        self._generator = generator

    async def refresh(
        self,
        server: McpServer,
        tools: list[McpToolDescriptor],
    ) -> SkillPattern | None:
        if self._generator is None or not tools:
            return None
        fingerprint = tools_fingerprint(tools)
        if fingerprint == server.tools_fingerprint:
            return None
        try:
            pattern = await self._generator.refresh(server, tools)
        except McpError as exc:
            logger.warning("MCP skill generation failed: server=%s error=%s", server.name, exc)
            return None
        await self._store.set_tools_fingerprint(server.id, fingerprint)
        return pattern
