"""Basic skill deleting one memory (per-user or global scope)."""

from typing import Any

from octoforge_core.memory.api import MemoryNotFoundError, MemoryScope, MemoryStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "memory_delete"
SKILL_DESCRIPTION = (
    "Delete one memory by key. Scope 'user' (default) targets this user's memory; "
    "scope 'global' targets a memory shared by all users."
)
DELETED_TEMPLATE = "memory '{key}' deleted (scope={scope})"
NOT_FOUND_TEMPLATE = "memory '{key}' not found (scope={scope})"
PARAMETERS_SCHEMA: dict[str, Any] = {
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


class MemoryDeleteSkill:
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


def _scope(raw: object) -> MemoryScope:
    if raw is None:
        return MemoryScope.USER
    if not isinstance(raw, str):
        raise SkillArgumentsError("scope must be 'user' or 'global'")
    try:
        return MemoryScope(raw)
    except ValueError as exc:
        raise SkillArgumentsError("scope must be 'user' or 'global'") from exc
