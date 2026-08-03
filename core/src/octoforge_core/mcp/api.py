"""Public boundary of the MCP module: external MCP servers as shared records.

An MCP server added by anyone becomes a capability of the whole installation:
the server row is deduplicated by URL, a link row records who added it, and
the server's tools are mirrored into public endpoint records
(``mcp/{server}/{tool}``) that the ordinary ``recall`` finds. The protocol
offers neither search nor a version of the tool list — only a paginated
``tools/list`` — which is why the mirror exists: it is a persistent cache of
``tools/list`` with embeddings, refreshed by a periodic sync rather than
checked before every call.

Transport is Streamable HTTP only. stdio is a decision, not a gap: a local
MCP server is an arbitrary executable, and "no shell, no runtime code
loading" stays true here.

Authentication is per-user by construction: the server record declares a
secret *code* (plus the header to carry it), and every user stores their own
value under that code in the secret store, host-bound as everywhere else.
There are no installation-global tokens.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

#: Title prefix of every mirrored tool record: the sync's own namespace.
MIRROR_PREFIX = "mcp/"
#: The content discriminator of a mirrored tool record.
MIRROR_KIND = "mcp"
#: The content discriminator is absent from classic endpoint records.
KIND_FIELD = "kind"

DEFAULT_AUTH_HEADER = "Authorization"
DEFAULT_AUTH_FORMAT = "Bearer {value}"


class McpError(Exception):
    """A failed MCP interaction: transport, protocol or server-reported."""


class McpServerNameTakenError(Exception):
    """Raised when the requested server name already points at another URL."""


@dataclass(frozen=True, slots=True)
class McpServer:
    """One external MCP server the installation knows.

    ``auth_secret_code`` names the per-user secret every subscriber stores
    their own token under; None means the server needs no credential.
    """

    id: str
    name: str
    url: str
    auth_secret_code: str | None = None
    auth_header: str = DEFAULT_AUTH_HEADER
    auth_format: str = DEFAULT_AUTH_FORMAT
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    #: Hash of the tool list the skill generator last ruled on.
    tools_fingerprint: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """One tool as the server describes it — third-party text, not gospel."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class McpToolResult:
    """Outcome of ``tools/call``; a server-reported error is data, not an exception."""

    text: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class McpMirror:
    """Executable view of a mirrored tool record's content."""

    server: str
    tool: str
    input_schema: dict[str, Any]


McpServerList = list[McpServer]
McpToolDescriptorList = list[McpToolDescriptor]


def parse_mirror(content: str) -> McpMirror:
    """Parse a mirrored tool record's content into its executable view."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise McpError(f"mirror record is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get(KIND_FIELD) != MIRROR_KIND:
        raise McpError("mirror record content is not an MCP tool contract")
    server = data.get("server")
    tool = data.get("tool")
    if not isinstance(server, str) or not server or not isinstance(tool, str) or not tool:
        raise McpError("mirror record must name its server and tool")
    schema = data.get("input_schema")
    return McpMirror(
        server=server,
        tool=tool,
        input_schema=schema if isinstance(schema, dict) else {},
    )


class McpServerStore(Protocol):
    """Port over the shared server rows and who-added-them links."""

    async def add(self, server: McpServer, user_id: str) -> McpServer:
        """Register the server for this user, deduplicating by URL.

        A URL the installation already knows gains a link, not a second row —
        two people adding the same server must end up sharing one record.
        Raises `McpServerNameTakenError` when the name is claimed by a
        different URL.
        """
        ...

    async def get_by_name(self, name: str) -> McpServer | None:
        """The server behind a mirror record's ``server`` field, or None."""
        ...

    async def list_all(self) -> McpServerList:
        """Every server, oldest first (the periodic sync walks this)."""
        ...

    async def list_user_ids(self, server_id: str) -> list[str]:
        """Who added the server, in link order (oldest first)."""
        ...

    async def mark_synced(self, server_id: str, error: str | None) -> None:
        """Record a sync attempt: its moment and its error (None on success)."""
        ...

    async def set_tools_fingerprint(self, server_id: str, fingerprint: str) -> None:
        """Record which tool list the skill generator has ruled on."""
        ...


class McpClient(Protocol):
    """Port over the wire protocol (Streamable HTTP JSON-RPC)."""

    async def list_tools(self, url: str, headers: dict[str, str]) -> McpToolDescriptorList:
        """Handshake and fetch the server's complete tool list."""
        ...

    async def call_tool(
        self,
        url: str,
        headers: dict[str, str],
        tool: str,
        arguments: dict[str, Any],
    ) -> McpToolResult:
        """Handshake and invoke one tool with the given arguments."""
        ...
