"""Configuration and value objects for mirrored MCP execution."""

from dataclasses import dataclass

from octoforge_core.net.collections.ingest import ResponseSpill
from octoforge_core.net.external import MAX_BODY_CHARS
from octoforge_core.secrets.api import SecretStore

SERVER_GONE_TEMPLATE = (
    "MCP server '{server}' is not registered on this installation; its tool records are stale"
)
TOOL_ERROR_TEMPLATE = "mcp tool error: {text}\nthe tool declares this contract: {contract}"


@dataclass(frozen=True, slots=True)
class McpExecutorOptions:
    secrets: SecretStore | None = None
    spill: ResponseSpill | None = None
    truncate_chars: int = MAX_BODY_CHARS


DEFAULT_EXECUTOR_OPTIONS = McpExecutorOptions()


@dataclass(frozen=True, slots=True)
class McpSpillRequest:
    body: str
    user_id: str | None
    scope: str
    source: str
