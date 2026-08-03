"""MCP client: JSON-RPC over Streamable HTTP against a scripted transport."""

import json
from typing import Any

import httpx
import pytest

from octoforge_core.mcp.api import McpError
from octoforge_core.mcp.client import (
    MAX_RESPONSE_BYTES,
    PROTOCOL_VERSION,
    StreamableHttpMcpClient,
)
from octoforge_core.net.guard import SsrfGuard

URL = "https://mcp.example.com/mcp"
# allowlisted origin: the guard then skips DNS resolution for the fake host
GUARD = SsrfGuard(allowed_prefixes=("https://mcp.example.com",))

TOOL = {
    "name": "get_weather",
    "description": "Weather for a city",
    "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}},
}


def rpc_result(request_id: int | None, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


class ScriptedServer:
    """Answers initialize/initialized like a real server, then the scripted method."""

    def __init__(self, answers: dict[str, list[dict[str, Any]]], sse: bool = False) -> None:
        self._answers = {method: list(items) for method, items in answers.items()}
        self._sse = sse
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content.decode())
        self.requests.append(message)
        self.headers.append(dict(request.headers))
        method = message.get("method")
        if method == "initialize":
            payload = rpc_result(message["id"], {"protocolVersion": PROTOCOL_VERSION})
            return self._respond(payload, session="session-1")
        if method == "notifications/initialized":
            return httpx.Response(202)
        queue = self._answers.get(method, [])
        if not queue:
            return httpx.Response(200, json=rpc_result(message.get("id"), {}))
        return self._respond({"id": message.get("id"), **queue.pop(0)})

    def _respond(self, payload: dict[str, Any], session: str | None = None) -> httpx.Response:
        headers = {"Mcp-Session-Id": session} if session else {}
        if not self._sse:
            return httpx.Response(200, json={"jsonrpc": "2.0", **payload}, headers=headers)
        body = (
            'data: {"jsonrpc": "2.0", "method": "notifications/progress"}\n\n'
            f"data: {json.dumps({'jsonrpc': '2.0', **payload})}\n\n"
        )
        return httpx.Response(
            200, text=body, headers={"content-type": "text/event-stream", **headers}
        )


def make_client(server: ScriptedServer) -> tuple[StreamableHttpMcpClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    return StreamableHttpMcpClient(http, GUARD), http


async def test_list_tools_handshakes_then_lists() -> None:
    server = ScriptedServer({"tools/list": [{"result": {"tools": [TOOL]}}]})
    client, http = make_client(server)

    tools = await client.list_tools(URL, {})

    await http.aclose()
    assert [tool.name for tool in tools] == ["get_weather"]
    assert tools[0].input_schema["properties"]["city"] == {"type": "string"}
    # the protocol's required order: initialize -> initialized -> the request
    assert [message.get("method") for message in server.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    # every request declares the pinned protocol version
    assert all(
        headers.get("mcp-protocol-version") == PROTOCOL_VERSION for headers in server.headers
    )
    # the session id from initialize rides on the follow-ups
    assert server.headers[1].get("mcp-session-id") == "session-1"
    assert server.headers[2].get("mcp-session-id") == "session-1"


async def test_list_tools_follows_pagination() -> None:
    second = {**TOOL, "name": "get_forecast"}
    server = ScriptedServer(
        {
            "tools/list": [
                {"result": {"tools": [TOOL], "nextCursor": "page-2"}},
                {"result": {"tools": [second]}},
            ]
        }
    )
    client, http = make_client(server)

    tools = await client.list_tools(URL, {})

    await http.aclose()
    assert [tool.name for tool in tools] == ["get_weather", "get_forecast"]
    cursors = [
        message.get("params", {}).get("cursor")
        for message in server.requests
        if message.get("method") == "tools/list"
    ]
    assert cursors == [None, "page-2"]


async def test_call_tool_renders_text_and_placeholders() -> None:
    server = ScriptedServer(
        {
            "tools/call": [
                {
                    "result": {
                        "content": [
                            {"type": "text", "text": "sunny"},
                            {"type": "image", "data": "...", "mimeType": "image/png"},
                        ],
                        "isError": False,
                    }
                }
            ]
        }
    )
    client, http = make_client(server)

    result = await client.call_tool(URL, {}, "get_weather", {"city": "Lisbon"})

    await http.aclose()
    assert result.text == "sunny\n[image content omitted]"
    assert result.is_error is False


async def test_sse_answers_are_parsed_past_interleaved_notifications() -> None:
    answers = {"tools/call": [{"result": {"content": [], "isError": True}}]}
    server = ScriptedServer(answers, sse=True)
    client, http = make_client(server)

    result = await client.call_tool(URL, {}, "anything", {})

    await http.aclose()
    assert result.is_error is True


async def test_jsonrpc_error_becomes_mcp_error() -> None:
    server = ScriptedServer(
        {"tools/call": [{"error": {"code": -32602, "message": "Unknown tool: nope"}}]}
    )
    client, http = make_client(server)

    with pytest.raises(McpError, match="Unknown tool"):
        await client.call_tool(URL, {}, "nope", {})
    await http.aclose()


async def test_http_error_status_becomes_mcp_error() -> None:
    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="who are you")

    http = httpx.AsyncClient(transport=httpx.MockTransport(deny))
    client = StreamableHttpMcpClient(http, GUARD)

    with pytest.raises(McpError, match="HTTP 401"):
        await client.list_tools(URL, {})
    await http.aclose()


async def test_oversized_response_is_refused_not_buffered() -> None:
    def flood(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    http = httpx.AsyncClient(transport=httpx.MockTransport(flood))
    client = StreamableHttpMcpClient(http, GUARD)

    with pytest.raises(McpError, match="size limit"):
        await client.list_tools(URL, {})
    await http.aclose()


async def test_auth_headers_ride_every_request() -> None:
    server = ScriptedServer({"tools/list": [{"result": {"tools": []}}]})
    client, http = make_client(server)

    await client.list_tools(URL, {"Authorization": "Bearer token-1"})

    await http.aclose()
    assert all(headers.get("authorization") == "Bearer token-1" for headers in server.headers)
