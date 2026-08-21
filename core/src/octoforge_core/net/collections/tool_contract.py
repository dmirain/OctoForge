"""Names, descriptions, and JSON schemas of collection tools."""

from typing import Any

from octoforge_core.net.collections.api import FilterOp, QueryOp

QUERY_NAME = "collection_query"
QUERY_DESCRIPTION = (
    "Query a collection created from a large HTTP response (ref like 'col:…' from the "
    "call's passport). Runs in the database, not in context: use it instead of asking "
    "for the raw body. Ops: get (records), pluck (one field of every record), count, "
    "sum/avg/min/max (optionally per group_by), distinct. Filters narrow every op; "
    "field paths are dotted (owner.city) and must exist in the passport's schema."
)
GET_NAME = "collection_get"
GET_DESCRIPTION = (
    "Re-read the passport of a collection (ref like 'col:…'): schema, record count, "
    "expiry. Use it when the passport has fallen out of context; an expired or "
    "unknown ref answers not-found — fetch the data again in that case."
)

QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Collection ref from the passport (col:…)"},
        "op": {
            "type": "string",
            "enum": [op.value for op in QueryOp],
            "description": "What to compute",
        },
        "field": {
            "type": "string",
            "description": "Dotted record field for pluck/sum/avg/min/max/distinct",
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "op": {"type": "string", "enum": [op.value for op in FilterOp]},
                    "value": {"type": ["string", "number", "boolean", "null"]},
                },
                "required": ["field", "op"],
            },
            "description": "Conditions every counted/returned record must satisfy",
        },
        "group_by": {
            "type": "string",
            "description": "Dotted field to group an aggregate by (count/sum/avg/min/max)",
        },
        "source": {
            "type": "string",
            "description": "Only records that arrived from this source tag",
        },
        "limit": {"type": "integer", "description": "Rows per page"},
        "offset": {"type": "integer", "description": "Rows to skip (paging)"},
        "max_chars": {
            "type": "integer",
            "description": (
                "How much of the rendered result you choose to take (the default "
                "is conservative; raise it deliberately for big reads)"
            ),
        },
    },
    "required": ["ref", "op"],
}
GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Collection ref (col:…)"},
    },
    "required": ["ref"],
}
