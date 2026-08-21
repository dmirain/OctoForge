"""Agent-facing contract of history_search."""

from typing import Any

TOOL_NAME = "history_search"
TOOL_DESCRIPTION = (
    "Search the full history of this conversation (including what fell out of the "
    "recent verbatim tail) by a case-insensitive substring over message content. "
    "Optionally restrict the hits to a topic tag of the compressed summaries or to "
    "a date range. Use it when the user refers to something discussed earlier that "
    "is not covered by the topic summaries or the recent tail."
)
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Substring to look for in message content"},
        "topic": {
            "type": "string",
            "description": "Restrict hits to the seq ranges of summaries tagged with this topic",
        },
        "date_from": {
            "type": "string",
            "description": "ISO date/datetime, inclusive lower bound (UTC when naive)",
        },
        "date_to": {
            "type": "string",
            "description": (
                "ISO date/datetime upper bound (UTC when naive): a date-only value "
                "is inclusive (covers the whole day), a datetime is exclusive"
            ),
        },
        "limit": {"type": "integer", "description": "How many messages to return"},
    },
    "required": ["query"],
}
