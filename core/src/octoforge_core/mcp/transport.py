"""Guarded, capped Streamable HTTP transport for MCP JSON-RPC messages."""

from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import Any

import httpx

from octoforge_core.config import DEFAULT_TIMEOUT_SECONDS
from octoforge_core.mcp.protocol import PROTOCOL_VERSION, last_sse_message, parse_json
from octoforge_core.mcp.types import McpError
from octoforge_core.net.external import read_capped_text
from octoforge_core.net.guard import SsrfGuard

PROTOCOL_VERSION_HEADER = "MCP-Protocol-Version"
SESSION_ID_HEADER = "Mcp-Session-Id"
ACCEPT = "application/json, text/event-stream"
SSE_CONTENT_TYPE = "text/event-stream"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class McpClientConfig:
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = MAX_RESPONSE_BYTES


@dataclass(frozen=True, slots=True)
class McpConnection:
    url: str
    headers: dict[str, str]
    session_id: str | None = None

    def with_session(self, session_id: str | None) -> "McpConnection":
        return replace(self, session_id=session_id)


@dataclass(frozen=True, slots=True)
class RpcResponse:
    message: dict[str, Any]
    session_id: str | None


class McpHttpTransport:
    """Send one guarded request and parse either JSON or an SSE response."""

    def __init__(self, http: httpx.AsyncClient, guard: SsrfGuard, config: McpClientConfig) -> None:
        self._http = http
        self._guard = guard
        self._config = config

    async def post(
        self,
        connection: McpConnection,
        payload: dict[str, Any],
        expect_body: bool = True,
    ) -> RpcResponse:
        await self._guard.check(connection.url)
        headers = {
            **connection.headers,
            "Accept": ACCEPT,
            "Content-Type": "application/json",
            PROTOCOL_VERSION_HEADER: PROTOCOL_VERSION,
        }
        if connection.session_id is not None:
            headers[SESSION_ID_HEADER] = connection.session_id
        try:
            async with self._http.stream(
                "POST",
                connection.url,
                json=payload,
                headers=headers,
                follow_redirects=False,
                timeout=self._config.timeout_seconds,
            ) as response:
                body, truncated = await read_capped_text(
                    response,
                    self._config.max_response_bytes,
                )
                status = response.status_code
                content_type = response.headers.get("content-type", "")
                session_id = response.headers.get(SESSION_ID_HEADER) or connection.session_id
        except httpx.HTTPError as exc:
            raise McpError(f"MCP request failed: {exc}") from exc
        if status >= HTTPStatus.BAD_REQUEST:
            raise McpError(f"MCP server answered HTTP {status}: {body[:200]}")
        if truncated:
            raise McpError("MCP response exceeded the size limit")
        if not expect_body:
            return RpcResponse({}, session_id)
        message = (
            last_sse_message(body)
            if content_type.startswith(SSE_CONTENT_TYPE)
            else parse_json(body)
        )
        return RpcResponse(message, session_id)
