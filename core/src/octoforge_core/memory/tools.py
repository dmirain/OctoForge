"""Tools of the memory module: store/search/delete over the MemoryStore port."""

from typing import Any

from octoforge_core.memory.api import Memory, MemoryNotFoundError, MemoryScope, MemoryStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

STORE_NAME = "memory_store"
STORE_DESCRIPTION = (
    "Store a durable memory under a key (upsert: an existing key of the same scope is "
    "replaced). Scope 'user' (default) is visible to this user on every surface; scope "
    "'global' is visible to all users — use it sparingly, for facts shared by everyone."
)
STORED_TEMPLATE = "memory stored (scope={scope}, key={key}, created={created})"
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
        "scope": {
            "type": "string",
            "enum": [scope.value for scope in MemoryScope],
            "description": "Who sees the memory: this user (default) or everyone",
        },
    },
    "required": ["key", "content"],
}

SEARCH_NAME = "memory_search"
SEARCH_DESCRIPTION = (
    "Search memories visible to this user (own entries plus global ones) by a "
    "case-insensitive substring over key and content; newest first. Use it before "
    "personal recommendations and whenever the user's durable facts may matter."
)
SNIPPET_CHARS = 300
NO_HITS_MESSAGE = "no memories found"
ENTRY_TEMPLATE = "[{scope}] {key} — {snippet} — tags: {tags}"
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Substring to look for in key/content"},
        "limit": {"type": "integer", "description": "How many memories to return"},
    },
    "required": ["query"],
}

DELETE_NAME = "memory_delete"
DELETE_DESCRIPTION = (
    "Delete one memory by key. Scope 'user' (default) targets this user's memory; "
    "scope 'global' targets a memory shared by all users."
)
DELETED_TEMPLATE = "memory '{key}' deleted (scope={scope})"
NOT_FOUND_TEMPLATE = "memory '{key}' not found (scope={scope})"
DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Memory key"},
        "scope": {
            "type": "string",
            "enum": [scope.value for scope in MemoryScope],
            "description": "Which memory to delete: this user's (default) or a global one",
        },
    },
    "required": ["key"],
}


class MemoryStoreSkill:
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=STORE_NAME,
            description=STORE_DESCRIPTION,
            parameters_schema=STORE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, upsert the memory and report created/updated."""
        key = _non_empty_string(arguments.get("key"), "key")
        content = _non_empty_string(arguments.get("content"), "content")
        tags = _tags(arguments.get("tags"))
        scope = _scope(arguments.get("scope"))
        owner = None if scope is MemoryScope.GLOBAL else context.user_id
        memory, created = await self._store.put(owner, key, content, tags)
        return STORED_TEMPLATE.format(
            scope=scope.value,
            key=memory.key,
            created="true" if created else "false",
        )


class MemorySearchSkill:
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore, default_limit: int, max_limit: int) -> None:
        self._store = store
        self._default_limit = default_limit
        self._max_limit = max_limit

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SEARCH_NAME,
            description=SEARCH_DESCRIPTION,
            parameters_schema=SEARCH_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, search and format the hits as numbered lines."""
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SkillArgumentsError("query must be a non-empty string")
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
            raise SkillArgumentsError("limit must be an integer")
        if raw < 1 or raw > self._max_limit:
            raise SkillArgumentsError(f"limit must be between 1 and {self._max_limit}")
        return raw


class MemoryDeleteSkill:
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=DELETE_NAME,
            description=DELETE_DESCRIPTION,
            parameters_schema=DELETE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, delete the memory and report the outcome as text."""
        key = arguments.get("key")
        if not isinstance(key, str) or not key.strip():
            raise SkillArgumentsError("key must be a non-empty string")
        scope = _scope(arguments.get("scope"))
        owner = None if scope is MemoryScope.GLOBAL else context.user_id
        try:
            await self._store.delete(owner, key)
        except MemoryNotFoundError:
            return NOT_FOUND_TEMPLATE.format(key=key, scope=scope.value)
        return DELETED_TEMPLATE.format(key=key, scope=scope.value)


def _non_empty_string(raw: object, argument: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SkillArgumentsError(f"{argument} must be a non-empty string")
    return raw


def _tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(tag, str) for tag in raw):
        raise SkillArgumentsError("tags must be an array of strings")
    return tuple(raw)


def _scope(raw: object) -> MemoryScope:
    if raw is None:
        return MemoryScope.USER
    if not isinstance(raw, str):
        raise SkillArgumentsError("scope must be 'user' or 'global'")
    try:
        return MemoryScope(raw)
    except ValueError as exc:
        raise SkillArgumentsError("scope must be 'user' or 'global'") from exc


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
