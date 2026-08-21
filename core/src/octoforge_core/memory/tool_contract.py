"""Model-facing names, descriptions, schemas and responses for memory tools."""

from typing import Any

STORE_NAME = "memory_store"
STORE_DESCRIPTION = (
    "Store a personal note or a durable fact about the user (birthdays, relatives, "
    'preferences - e.g. "my wife\'s birthday is March 5") under a key (upsert: an '
    "existing key is replaced). The memory belongs to this user and follows them "
    "across every surface; it is never shared with other users. Facts useful to "
    "everyone are saved as knowledge records via instruction_save instead. "
    "Stored memories come back through recall."
)
STORED_TEMPLATE = "memory stored (key={key}, version={version})"
MEMORY_LIMIT_REFUSAL_TEMPLATE = (
    "cannot store: this would take the memory to {projected} of the plan's "
    "{limit} characters. Delete something via memory_delete, shorten the "
    "content, or upgrade to a bigger plan."
)
STORE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Memory key (upsert target)"},
        "content": {"type": "string", "description": "Memory content"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional tags",
        },
    },
    "required": ["key", "content"],
}

DELETE_NAME = "memory_delete"
DELETE_DESCRIPTION = "Delete one of this user's memories by key."
DELETED_TEMPLATE = "memory '{key}' deleted"
NOT_FOUND_TEMPLATE = "memory '{key}' not found"
DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"key": {"type": "string", "description": "Memory key"}},
    "required": ["key"],
}
