"""Persistence and wire protocol ports of the MCP module."""

from typing import Protocol

from octoforge_core.mcp.types import (
    McpServer,
    McpServerList,
    McpToolCall,
    McpToolDescriptorList,
    McpToolResult,
)


class McpServerStore(Protocol):
    """Shared server rows and the users subscribed to each one."""

    async def add(self, server: McpServer, user_id: str) -> McpServer:
        """Register a server for a user, deduplicating by URL."""
        ...

    async def get_by_name(self, name: str) -> McpServer | None:
        """Return the named server, or None."""
        ...

    async def list_all(self) -> McpServerList:
        """Return every server, oldest first."""
        ...

    async def list_user_ids(self, server_id: str) -> list[str]:
        """Return server subscribers in link order."""
        ...

    async def mark_synced(self, server_id: str, error: str | None) -> None:
        """Record a sync attempt and optional failure."""
        ...

    async def set_tools_fingerprint(self, server_id: str, fingerprint: str) -> None:
        """Record which tool list the skill generator has ruled on."""
        ...


class McpClient(Protocol):
    """Streamable HTTP MCP wire operations."""

    async def list_tools(self, url: str, headers: dict[str, str]) -> McpToolDescriptorList:
        """Handshake and fetch the complete tool list."""
        ...

    async def call_tool(self, request: McpToolCall) -> McpToolResult:
        """Handshake and invoke one tool."""
        ...
