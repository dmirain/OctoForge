"""Minimal MCP client: JSON-RPC 2.0 over Streamable HTTP.

Hand-rolled over the shared httpx client on purpose — the official SDK owns
its transport, which would route requests around the SSRF guard, the
no-redirect rule and the capped reads that every other outbound call obeys
(and it drags a server-side dependency tree through the pip-audit gate for
the three methods used here). The same trade the LLM client already made.

The protocol requires ``initialize`` → ``notifications/initialized`` before
anything else; this client performs that handshake lazily per operation and
reuses the ``Mcp-Session-Id`` within it. One extra round-trip against an LLM
turn measured in seconds — long-lived pooled sessions are a complication the
one-process actor host does not need.

A server may answer a POST with plain JSON or with an SSE stream; both are
read through the same byte cap before parsing.
"""

import json
import logging
from typing import Any

import httpx

from octoforge_core.config import DEFAULT_TIMEOUT_SECONDS
from octoforge_core.mcp.api import McpError, McpToolDescriptor, McpToolDescriptorList, McpToolResult
from octoforge_core.net.external import read_capped_text
from octoforge_core.net.guard import SsrfGuard

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
SESSION_ID_HEADER = "Mcp-Session-Id"
CLIENT_INFO = {"name": "octoforge", "version": "1.0"}
ACCEPT = "application/json, text/event-stream"
SSE_CONTENT_TYPE = "text/event-stream"
SSE_DATA_PREFIX = "data:"
#: Byte ceiling for any single response, tools/list included: a hostile
#: server must not flood the sync (mirrors net/external.MAX_BODY_BYTES).
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
#: tools/list pagination is followed at most this far — a runaway server
#: handing out endless cursors must terminate anyway.
MAX_LIST_PAGES = 20
#: Placeholder rendered for non-text content blocks of a tool result.
NON_TEXT_TEMPLATE = "[{type} content omitted]"


class StreamableHttpMcpClient:
    """`McpClient` over Streamable HTTP with the platform's egress discipline."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        guard: SsrfGuard,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._http = http_client
        self._guard = guard
        self._timeout = timeout_seconds

    async def list_tools(self, url: str, headers: dict[str, str]) -> McpToolDescriptorList:
        """Handshake and fetch the complete tool list, following pagination."""
        session = await self._handshake(url, headers)
        tools: McpToolDescriptorList = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor is not None else {}
            result = await self._request(url, headers, session, "tools/list", params)
            tools.extend(_parse_tool(raw) for raw in _expect_list(result, "tools"))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            cursor = next_cursor
        return tools

    async def call_tool(
        self,
        url: str,
        headers: dict[str, str],
        tool: str,
        arguments: dict[str, Any],
    ) -> McpToolResult:
        """Handshake and invoke one tool; a server-side tool error is data."""
        session = await self._handshake(url, headers)
        result = await self._request(
            url, headers, session, "tools/call", {"name": tool, "arguments": arguments}
        )
        blocks = result.get("content")
        text = _render_blocks(blocks) if isinstance(blocks, list) else ""
        return McpToolResult(text=text, is_error=bool(result.get("isError")))

    async def _handshake(self, url: str, headers: dict[str, str]) -> str | None:
        """`initialize` → `notifications/initialized`; returns the session id."""
        response = await self._post(
            url,
            headers,
            session=None,
            payload=_rpc_request(
                1,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            ),
        )
        _expect_result(response.message, "initialize")
        session = response.session_id
        await self._post(
            url,
            headers,
            session,
            payload={"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_body=False,
        )
        return session

    async def _request(
        self,
        url: str,
        headers: dict[str, str],
        session: str | None,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._post(url, headers, session, _rpc_request(2, method, params))
        return _expect_result(response.message, method)

    async def _post(
        self,
        url: str,
        headers: dict[str, str],
        session: str | None,
        payload: dict[str, Any],
        expect_body: bool = True,
    ) -> "_RpcResponse":
        """One guarded POST; the response may be JSON or a capped SSE stream."""
        await self._guard.check(url)
        request_headers = {
            **headers,
            "Accept": ACCEPT,
            "Content-Type": "application/json",
            PROTOCOL_VERSION_HEADER: PROTOCOL_VERSION,
        }
        if session is not None:
            request_headers[SESSION_ID_HEADER] = session
        try:
            async with self._http.stream(
                "POST",
                url,
                json=payload,
                headers=request_headers,
                follow_redirects=False,  # a redirect would bypass the guard's URL check
                timeout=self._timeout,
            ) as response:
                body, truncated = await read_capped_text(response, MAX_RESPONSE_BYTES)
                status = response.status_code
                content_type = response.headers.get("content-type", "")
                session_id = response.headers.get(SESSION_ID_HEADER)
        except httpx.HTTPError as exc:
            raise McpError(f"MCP request failed: {exc}") from exc
        if status >= 400:  # noqa: PLR2004 — HTTP client-error boundary
            raise McpError(f"MCP server answered HTTP {status}: {body[:200]}")
        if truncated:
            raise McpError("MCP response exceeded the size limit")
        if not expect_body:
            return _RpcResponse(message={}, session_id=session_id or session)
        message = (
            _last_sse_message(body)
            if content_type.startswith(SSE_CONTENT_TYPE)
            else _parse_json(body)
        )
        return _RpcResponse(message=message, session_id=session_id or session)


class _RpcResponse:
    """One parsed JSON-RPC answer plus the session id the transport carried."""

    __slots__ = ("message", "session_id")

    def __init__(self, message: dict[str, Any], session_id: str | None) -> None:
        self.message = message
        self.session_id = session_id


def _rpc_request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _parse_json(body: str) -> dict[str, Any]:
    try:
        message = json.loads(body)
    except json.JSONDecodeError as exc:
        raise McpError(f"MCP server answered with invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise McpError("MCP server answered with a non-object JSON-RPC message")
    return message


def _last_sse_message(body: str) -> dict[str, Any]:
    """The last JSON-RPC message of an SSE answer.

    A Streamable HTTP server may interleave notifications before the actual
    response; the response to the POSTed request is the last `data:` event
    carrying a message with a result or error.
    """
    message: dict[str, Any] | None = None
    for event in body.split("\n\n"):
        data_lines = [
            line[len(SSE_DATA_PREFIX) :].strip()
            for line in event.splitlines()
            if line.startswith(SSE_DATA_PREFIX)
        ]
        if not data_lines:
            continue
        try:
            candidate = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
            message = candidate
    if message is None:
        raise McpError("MCP server's event stream carried no JSON-RPC response")
    return message


def _expect_result(message: dict[str, Any], method: str) -> dict[str, Any]:
    error = message.get("error")
    if isinstance(error, dict):
        raise McpError(f"MCP {method} failed: {error.get('message', 'unknown error')}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpError(f"MCP {method} answered without a result")
    return result


def _expect_list(result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = result.get(key)
    if not isinstance(raw, list):
        raise McpError(f"MCP result carries no {key!r} list")
    return [item for item in raw if isinstance(item, dict)]


def _parse_tool(raw: dict[str, Any]) -> McpToolDescriptor:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise McpError("MCP tool descriptor without a name")
    description = raw.get("description")
    schema = raw.get("inputSchema")
    return McpToolDescriptor(
        name=name,
        description=description if isinstance(description, str) else "",
        input_schema=schema if isinstance(schema, dict) else {},
    )


def _render_blocks(blocks: list[Any]) -> str:
    """Concatenate text blocks; anything else becomes a placeholder."""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(NON_TEXT_TEMPLATE.format(type=block.get("type", "unknown")))
    return "\n".join(parts)
