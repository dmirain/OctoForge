"""Basic skill searching memories visible to the current user (own + global)."""

from typing import Any

from octoforge_core.memory.api import Memory, MemoryScope, MemoryStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "memory_search"
SKILL_DESCRIPTION = (
    "Search memories visible to this user (own entries plus global ones) by a "
    "case-insensitive substring over key and content; newest first. Use it before "
    "personal recommendations and whenever the user's durable facts may matter."
)
SNIPPET_CHARS = 300
NO_HITS_MESSAGE = "no memories found"
ENTRY_TEMPLATE = "[{scope}] {key} — {snippet} — tags: {tags}"
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Substring to look for in key/content"},
        "limit": {"type": "integer", "description": "How many memories to return"},
    },
    "required": ["query"],
}


class MemorySearchSkill:
    """Thin adapter over the MemoryStore port."""

    def __init__(self, store: MemoryStore, default_limit: int, max_limit: int) -> None:
        self._store = store
        self._default_limit = default_limit
        self._max_limit = max_limit

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
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
