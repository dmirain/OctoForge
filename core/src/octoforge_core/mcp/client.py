"""Minimal Streamable HTTP MCP client over the guarded platform transport."""

from typing import Any

import httpx

from octoforge_core.mcp.protocol import (
    PROTOCOL_VERSION,
    expect_list,
    expect_result,
    parse_tool,
    render_blocks,
    rpc_request,
)
from octoforge_core.mcp.transport import (
    MAX_RESPONSE_BYTES,
    McpClientConfig,
    McpConnection,
    McpHttpTransport,
)
from octoforge_core.mcp.types import McpToolCall, McpToolDescriptorList, McpToolResult
from octoforge_core.net.guard import SsrfGuard

CLIENT_INFO = {"name": "octoforge", "version": "1.0"}
MAX_LIST_PAGES = 20


DEFAULT_CLIENT_CONFIG = McpClientConfig()

__all__ = [
    "MAX_RESPONSE_BYTES",
    "PROTOCOL_VERSION",
    "McpClientConfig",
    "StreamableHttpMcpClient",
]


class StreamableHttpMcpClient:
    """Handshake per operation, then list or call tools over one MCP session."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        guard: SsrfGuard,
        config: McpClientConfig = DEFAULT_CLIENT_CONFIG,
    ) -> None:
        self._transport = McpHttpTransport(http_client, guard, config)

    async def list_tools(self, url: str, headers: dict[str, str]) -> McpToolDescriptorList:
        connection = await self._handshake(McpConnection(url, headers))
        tools: McpToolDescriptorList = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor is not None else {}
            result = await self._request(connection, "tools/list", params)
            tools.extend(parse_tool(raw) for raw in expect_list(result, "tools"))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            cursor = next_cursor
        return tools

    async def call_tool(self, request: McpToolCall) -> McpToolResult:
        connection = await self._handshake(McpConnection(request.url, request.headers))
        result = await self._request(
            connection,
            "tools/call",
            {"name": request.tool, "arguments": request.arguments},
        )
        blocks = result.get("content")
        text = render_blocks(blocks) if isinstance(blocks, list) else ""
        return McpToolResult(text=text, is_error=bool(result.get("isError")))

    async def _handshake(self, connection: McpConnection) -> McpConnection:
        response = await self._transport.post(
            connection,
            rpc_request(
                1,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            ),
        )
        expect_result(response.message, "initialize")
        connected = connection.with_session(response.session_id)
        await self._transport.post(
            connected,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_body=False,
        )
        return connected

    async def _request(
        self,
        connection: McpConnection,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._transport.post(connection, rpc_request(2, method, params))
        return expect_result(response.message, method)
