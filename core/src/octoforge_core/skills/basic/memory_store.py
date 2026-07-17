"""Basic skill storing durable memories (per-user or global scope)."""

from typing import Any

from octoforge_core.memory.api import MemoryScope, MemoryStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "memory_store"
SKILL_DESCRIPTION = (
    "Store a durable memory under a key (upsert: an existing key of the same scope is "
    "replaced). Scope 'user' (default) is visible to this user on every surface; scope "
    "'global' is visible to all users — use it sparingly, for facts shared by everyone."
)
STORED_TEMPLATE = "memory stored (scope={scope}, key={key}, created={created})"
PARAMETERS_SCHEMA: dict[str, Any] = {
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


class MemoryStoreSkill:
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
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
