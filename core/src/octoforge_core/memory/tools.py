"""Tools of the memory module: store/delete over the shared instruction store.

Memory is the per-user slice of the instruction store (`InstructionType.MEMORY`):
a memory is structurally a private knowledge record whose title is the memory
key. The tools here are intent-shaped wrappers — "remember this about the user"
is a different intent than "save shared knowledge", so the tool names survive
the storage merge. Reading goes through the shared search (`recall`),
which ranks memories with the same embeddings machinery as everything else;
a dedicated memory_search tool no longer exists.
"""

from typing import Any

from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
)
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

STORE_NAME = "memory_store"
STORE_DESCRIPTION = (
    "Store a personal note or a durable fact about the user (birthdays, relatives, "
    'preferences — e.g. "my wife\'s birthday is March 5") under a key (upsert: an '
    "existing key is replaced). The memory belongs to this user and follows them "
    "across every surface; it is never shared with other users. Facts useful to "
    "everyone are saved as knowledge records via instruction_save instead. "
    "Stored memories come back through recall."
)
STORED_TEMPLATE = "memory stored (key={key}, version={version})"
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
    "properties": {
        "key": {"type": "string", "description": "Memory key"},
    },
    "required": ["key"],
}


class MemoryStoreTool:
    """Intent-shaped wrapper over the shared instruction store (type=memory)."""

    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=STORE_NAME,
            description=STORE_DESCRIPTION,
            parameters_schema=STORE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments and upsert the caller's memory record.

        Writes are always user-scoped: the global scope was removed from the
        agent-facing tool — the sanctioned path to shared facts is a knowledge
        record plus the admin's publish, not an instantly-global write that any
        user could use to color every other user's answers.
        """
        key = _non_empty_string(arguments.get("key"), "key")
        content = _non_empty_string(arguments.get("content"), "content")
        tags = _tags(arguments.get("tags"))
        record = await self._service.save(
            context.user_id, InstructionType.MEMORY, key, content, tags
        )
        return STORED_TEMPLATE.format(key=record.title, version=record.version)


class MemoryDeleteTool:
    """Deletes the caller's own memory record by key."""

    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=DELETE_NAME,
            description=DELETE_DESCRIPTION,
            parameters_schema=DELETE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Resolve the caller's memory by key and delete it; not-found is text."""
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ToolArgumentsError("key must be a non-empty string")
        record = await self._find_own(key, context.user_id)
        if record is None:
            return NOT_FOUND_TEMPLATE.format(key=key)
        try:
            await self._service.delete(context.user_id, record.id)
        except InstructionNotFoundError:
            return NOT_FOUND_TEMPLATE.format(key=key)
        return DELETED_TEMPLATE.format(key=key)

    async def _find_own(self, key: str, user_id: str) -> Instruction | None:
        """The caller's own memory record, never a public/legacy one."""
        try:
            record = await self._service.get_by_name(key, InstructionType.MEMORY, user_id)
        except InstructionNotFoundError:
            return None
        return record if record.owner_id == user_id else None


def _non_empty_string(raw: object, argument: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{argument} must be a non-empty string")
    return raw


def _tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(tag, str) for tag in raw):
        raise ToolArgumentsError("tags must be an array of strings")
    return tuple(raw)
