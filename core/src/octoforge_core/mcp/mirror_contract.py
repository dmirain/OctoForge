"""Parsing of persisted MCP mirror records."""

import json

from octoforge_core.mcp.types import McpError, McpMirror

MIRROR_PREFIX = "mcp/"
MIRROR_KIND = "mcp"
KIND_FIELD = "kind"


def parse_mirror(content: str) -> McpMirror:
    """Parse a mirrored endpoint record into its executable view."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise McpError(f"mirror record is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get(KIND_FIELD) != MIRROR_KIND:
        raise McpError("mirror record content is not an MCP tool contract")
    server = data.get("server")
    tool = data.get("tool")
    if not isinstance(server, str) or not server:
        raise McpError("mirror record must name its server and tool")
    if not isinstance(tool, str) or not tool:
        raise McpError("mirror record must name its server and tool")
    schema = data.get("input_schema")
    return McpMirror(server, tool, schema if isinstance(schema, dict) else {})
