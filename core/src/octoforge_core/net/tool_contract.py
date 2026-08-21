"""Model-facing contracts of outbound HTTP tools."""

from enum import StrEnum
from typing import Any

REQUEST_NAME = "http_request"
REQUEST_DESCRIPTION = (
    "Perform an HTTP request and return status and body. Use this fallback only "
    "when no stored endpoint covers the call; never guess API URLs or parameters."
)
CALL_NAME = "external_call"
CALL_DESCRIPTION = (
    "Execute a stored endpoint contract. Resolve it with endpoint_get before the "
    "first call and pass exactly its declared params. MCP records accept structured values."
)
ENDPOINT_GET_NAME = "endpoint_get"
ENDPOINT_GET_DESCRIPTION = (
    "Resolve an endpoint by exact name and return its contract. Discover unknown "
    "endpoint names with recall(type=endpoint)."
)
ENDPOINT_NOT_FOUND_TEMPLATE = (
    "endpoint '{name}' not found; discover endpoints with recall(type=endpoint, query=...)"
)
ENDPOINT_TEMPLATE = "[endpoint] {title}\n   tags: {tags}\n{content}"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    PROPFIND = "PROPFIND"
    PROPPATCH = "PROPPATCH"
    REPORT = "REPORT"
    MKCOL = "MKCOL"
    MKCALENDAR = "MKCALENDAR"
    COPY = "COPY"
    MOVE = "MOVE"


REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": [method.value for method in HttpMethod]},
        "url": {"type": "string", "description": "Full URL including scheme"},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "body": {"type": "string", "description": "Optional request body"},
    },
    "required": ["method", "url"],
}
CALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Endpoint title"},
        "params": {"type": "object", "description": "Contract parameters"},
        "collect": {
            "type": "boolean",
            "description": "Walk declared pagination into one collection",
        },
        "max_pages": {
            "type": "integer",
            "description": "Lower the configured collection page ceiling",
        },
        "into": {
            "type": "string",
            "description": "Append records to an existing col: reference",
        },
        "label": {"type": "string", "description": "Name a new collection"},
    },
    "required": ["name"],
}
ENDPOINT_GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"name": {"type": "string", "description": "Exact endpoint title"}},
    "required": ["name"],
}
