"""Tools of the memory module: store/search/delete over the MemoryStore port."""

from typing import Any

from octoforge_core.memory.api import Memory, MemoryNotFoundError, MemoryScope, MemoryStore
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

STORE_NAME = "memory_store"
STORE_DESCRIPTION = (
    "Store a personal note or a durable fact about the user (birthdays, relatives, "
    'preferences — e.g. "my wife\'s birthday is March 5") under a key (upsert: an '
    "existing key is replaced). The memory belongs to this user and follows them "
    "across every surface; it is never shared with other users. Facts useful to "
    "everyone are saved as knowledge records via instruction_save instead."
)
STORED_TEMPLATE = "memory stored (key={key}, created={created})"
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

SEARCH_NAME = "memory_search"
SEARCH_DESCRIPTION = (
    "Read what you remember about this user (own entries plus global ones), newest "
    "first. Call it with NO query to list the whole memory — it is short, and this is "
    "how you learn what is stored before you answer from assumptions; pass a query "
    "only to filter by a case-insensitive substring over key and content. Cheap and "
    "local: call it whenever the answer may depend on the user personally (personal "
    "recommendations, their setup, past decisions), not only when you already know a "
    "matching memory exists."
)
SNIPPET_CHARS = 300
NO_HITS_MESSAGE = "no memories found"
ENTRY_TEMPLATE = "[{scope}] {key} — {snippet} — tags: {tags}"
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Optional substring to look for in key/content; omit it to list every memory"
            ),
        },
        "limit": {"type": "integer", "description": "How many memories to return"},
    },
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
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=STORE_NAME,
            description=STORE_DESCRIPTION,
            parameters_schema=STORE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, upsert the caller's memory and report created/updated.

        Writes are always user-scoped: the global scope was removed from the
        agent-facing tool — the sanctioned path to shared facts is a knowledge
        record plus the admin's publish, not an instantly-global write that any
        user could use to color every other user's answers.
        """
        key = _non_empty_string(arguments.get("key"), "key")
        content = _non_empty_string(arguments.get("content"), "content")
        tags = _tags(arguments.get("tags"))
        memory, created = await self._store.put(context.user_id, key, content, tags)
        return STORED_TEMPLATE.format(
            key=memory.key,
            created="true" if created else "false",
        )


class MemorySearchTool:
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore, default_limit: int, max_limit: int) -> None:
        self._store = store
        self._default_limit = default_limit
        self._max_limit = max_limit

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=SEARCH_NAME,
            description=SEARCH_DESCRIPTION,
            parameters_schema=SEARCH_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, search and format the hits as numbered lines.

        An omitted (or blank) query is the catalog request: the store then
        returns the newest visible memories unfiltered.
        """
        raw_query = arguments.get("query")
        if raw_query is not None and not isinstance(raw_query, str):
            raise ToolArgumentsError("query must be a string")
        query = raw_query or ""
        limit = self._limit(arguments.get("limit"))
        memories = await self._store.search(context.user_id, query, limit)
        if not memories:
            return NO_HITS_MESSAGE
        return "\n".join(
            f"{index}. {_format_entry(memory)}" for index, memory in enumerate(memories, start=1)
        )

    def _limit(self, raw: object) -> int:
        if raw is None:
            return self._default_limit
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ToolArgumentsError("limit must be an integer")
        if raw < 1 or raw > self._max_limit:
            raise ToolArgumentsError(f"limit must be between 1 and {self._max_limit}")
        return raw


class MemoryDeleteTool:
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=DELETE_NAME,
            description=DELETE_DESCRIPTION,
            parameters_schema=DELETE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, delete the caller's memory and report the outcome as text."""
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip():
            raise ToolArgumentsError("key must be a non-empty string")
        try:
            await self._store.delete(context.user_id, key)
        except MemoryNotFoundError:
            return NOT_FOUND_TEMPLATE.format(key=key)
        return DELETED_TEMPLATE.format(key=key)


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


def _format_entry(memory: Memory) -> str:
    scope = MemoryScope.GLOBAL.value if memory.user_id is None else MemoryScope.USER.value
    return ENTRY_TEMPLATE.format(
        scope=scope,
        key=memory.key,
        snippet=_snippet(memory.content),
        tags=", ".join(memory.tags) if memory.tags else "-",
    )


def _snippet(content: str) -> str:
    one_line = " ".join(content.split())
    return one_line[:SNIPPET_CHARS]
