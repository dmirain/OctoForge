"""MCP server, tool, call and result values."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

DEFAULT_AUTH_HEADER = "Authorization"
DEFAULT_AUTH_FORMAT = "Bearer {value}"


class McpError(Exception):
    """A failed MCP interaction: transport, protocol or server-reported."""


class McpServerNameTakenError(Exception):
    """Raised when a requested name already points at another URL."""


@dataclass(frozen=True, slots=True)
class McpServer:
    """One external MCP server known to the installation."""

    id: str
    name: str
    url: str
    auth_secret_code: str | None = None
    auth_header: str = DEFAULT_AUTH_HEADER
    auth_format: str = DEFAULT_AUTH_FORMAT
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    tools_fingerprint: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """One tool as the external server describes it."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class McpToolCall:
    """Complete input to one tools/call protocol operation."""

    url: str
    headers: dict[str, str]
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class McpToolResult:
    """Outcome of tools/call; server-reported failure remains data."""

    text: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class McpMirror:
    """Executable view of a mirrored endpoint record."""

    server: str
    tool: str
    input_schema: dict[str, Any]


McpServerList = list[McpServer]
McpToolDescriptorList = list[McpToolDescriptor]
