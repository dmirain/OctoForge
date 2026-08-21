"""Call one mirrored MCP tool and translate protocol errors for external_call."""

from dataclasses import dataclass
from typing import Any

from octoforge_core.mcp.api import McpClient, McpError, McpServer, McpToolCall, McpToolResult
from octoforge_core.net.errors import ExternalCallError

STALE_MIRROR_HINT = (
    "the mirrored contract may be stale — the periodic sync refreshes it, or re-run mcp_add"
)


@dataclass(frozen=True, slots=True)
class McpInvocation:
    headers: dict[str, str]
    tool: str
    params: dict[str, Any]
    contract: str


async def invoke(client: McpClient, server: McpServer, invocation: McpInvocation) -> McpToolResult:
    try:
        return await client.call_tool(
            McpToolCall(server.url, invocation.headers, invocation.tool, invocation.params)
        )
    except McpError as exc:
        raise ExternalCallError(
            f"MCP call failed: {exc}; {STALE_MIRROR_HINT}; "
            f"the tool declares this contract: {invocation.contract}"
        ) from exc
