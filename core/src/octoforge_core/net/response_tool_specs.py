"""Model-facing names, descriptions, and schemas for response reading tools."""

from typing import Any

GET_NAME = "response_get"
FIND_NAME = "response_find"
WINDOW_NAME = "response_window"

GET_DESCRIPTION = (
    "Read a remembered response (ref 'resp:…' from a call's passport): the whole "
    "body, or one key of a JSON document (dotted path). The passport lists sizes — "
    "decide from them how much context to spend and pass max_chars accordingly; "
    "the default is conservative. For documents too big for any budget, search "
    "with response_find instead of reading."
)
FIND_DESCRIPTION = (
    "Find a literal substring inside a remembered response ('resp:…'), "
    "case-insensitive, with a window of characters around every occurrence. "
    "Answers the total count, and each match carries its 'at' position — widen a "
    "specific spot with response_window(at). Use a longer, more distinctive "
    "substring rather than paging when there are too many matches. Not a regex: "
    "pass the exact text you expect to see."
)
WINDOW_DESCRIPTION = (
    "A window of a remembered response ('resp:…') around a position that "
    "response_find returned ('at'): more characters before/after the same spot."
)

GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Response ref (resp:…)"},
        "key": {
            "type": "string",
            "description": "Dotted key of a JSON document; omit for the whole body",
        },
        "max_chars": {
            "type": "integer",
            "description": "How much you are choosing to read (see the passport's sizes)",
        },
    },
    "required": ["ref"],
}
FIND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Response ref (resp:…)"},
        "pattern": {
            "type": "string",
            "description": "Literal text to find (case-insensitive); not a regex",
        },
        "key": {
            "type": "string",
            "description": "Search inside this key's text instead of the whole body",
        },
        "before": {
            "type": "integer",
            "description": "Window chars before each match (default 300)",
        },
        "after": {"type": "integer", "description": "Window chars after each match (default 700)"},
        "max_matches": {"type": "integer", "description": "Matches per answer (default 5)"},
        "match_offset": {"type": "integer", "description": "Skip this many matches (paging)"},
    },
    "required": ["ref", "pattern"],
}
WINDOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Response ref (resp:…)"},
        "at": {"type": "integer", "description": "Position from a response_find match"},
        "key": {"type": "string", "description": "The key the find searched, if any"},
        "before": {"type": "integer", "description": "Chars before the position (default 1000)"},
        "after": {"type": "integer", "description": "Chars after the position (default 3000)"},
    },
    "required": ["ref", "at"],
}

MAX_PATTERN_CHARS = 512
FIND_DEFAULT_BEFORE = 300
FIND_DEFAULT_AFTER = 700
FIND_DEFAULT_MATCHES = 5
FIND_MAX_MATCHES = 20
WINDOW_DEFAULT_BEFORE = 1000
WINDOW_DEFAULT_AFTER = 3000
