"""JSON-RPC and SSE parsing for Streamable HTTP MCP responses."""

import json
from typing import Any

from octoforge_core.mcp.types import McpError, McpToolDescriptor

PROTOCOL_VERSION = "2025-06-18"
SSE_DATA_PREFIX = "data:"
NON_TEXT_TEMPLATE = "[{type} content omitted]"


def rpc_request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def parse_json(body: str) -> dict[str, Any]:
    try:
        message = json.loads(body)
    except json.JSONDecodeError as exc:
        raise McpError(f"MCP server answered with invalid JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise McpError("MCP server answered with a non-object JSON-RPC message")
    return message


def last_sse_message(body: str) -> dict[str, Any]:
    """Return the final response event, ignoring interleaved notifications."""
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


def expect_result(message: dict[str, Any], method: str) -> dict[str, Any]:
    error = message.get("error")
    if isinstance(error, dict):
        raise McpError(f"MCP {method} failed: {error.get('message', 'unknown error')}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpError(f"MCP {method} answered without a result")
    return result


def expect_list(result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = result.get(key)
    if not isinstance(raw, list):
        raise McpError(f"MCP result carries no {key!r} list")
    return [item for item in raw if isinstance(item, dict)]


def parse_tool(raw: dict[str, Any]) -> McpToolDescriptor:
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


def render_blocks(blocks: list[Any]) -> str:
    """Concatenate text blocks and replace other content with placeholders."""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(NON_TEXT_TEMPLATE.format(type=block.get("type", "unknown")))
    return "\n".join(parts)
