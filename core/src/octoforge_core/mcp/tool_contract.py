"""Model-facing contract and responses for mcp_add."""

from typing import Any

from octoforge_core.mcp.api import DEFAULT_AUTH_FORMAT, DEFAULT_AUTH_HEADER

NAME = "mcp_add"
DESCRIPTION = (
    "Register an external MCP server (Streamable HTTP URL) and mirror its tools into "
    "endpoint records. The same URL added twice stays one shared server. Discover "
    "tools with recall(type=endpoint), read endpoint_get, and execute external_call. "
    "Authentication names a per-user secret code; there are no shared credentials."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Streamable HTTP endpoint, e.g. https://mcp.example.com/mcp",
        },
        "name": {"type": "string", "description": "Short server slug"},
        "auth": {
            "type": "object",
            "description": "Only for servers requiring a token",
            "properties": {
                "secret": {"type": "string", "description": "Per-user secret code"},
                "header": {
                    "type": "string",
                    "description": f"Header name (default {DEFAULT_AUTH_HEADER})",
                },
                "format": {
                    "type": "string",
                    "description": f"Value template (default {DEFAULT_AUTH_FORMAT!r})",
                },
            },
            "required": ["secret"],
        },
    },
    "required": ["url"],
}

MAX_LISTED_TOOLS = 20
SECRET_HINT_TEMPLATE = (
    "the server declares auth via secret '{code}': each user adds their own "
    "token with /secrets before calling"
)
SYNC_FAILED_TEMPLATE = (
    "MCP server '{name}' registered, but listing its tools failed: {error}. "
    "The periodic sync will retry; fix the cause and re-run mcp_add."
)
