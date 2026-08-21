"""Public boundary of the MCP module."""

from octoforge_core.mcp.mirror_contract import (
    KIND_FIELD,
    MIRROR_KIND,
    MIRROR_PREFIX,
    parse_mirror,
)
from octoforge_core.mcp.ports import McpClient, McpServerStore
from octoforge_core.mcp.types import (
    DEFAULT_AUTH_FORMAT,
    DEFAULT_AUTH_HEADER,
    McpError,
    McpMirror,
    McpServer,
    McpServerList,
    McpServerNameTakenError,
    McpToolCall,
    McpToolDescriptor,
    McpToolDescriptorList,
    McpToolResult,
)

__all__ = [
    "DEFAULT_AUTH_FORMAT",
    "DEFAULT_AUTH_HEADER",
    "KIND_FIELD",
    "MIRROR_KIND",
    "MIRROR_PREFIX",
    "McpClient",
    "McpError",
    "McpMirror",
    "McpServer",
    "McpServerList",
    "McpServerNameTakenError",
    "McpServerStore",
    "McpToolCall",
    "McpToolDescriptor",
    "McpToolDescriptorList",
    "McpToolResult",
    "parse_mirror",
]
