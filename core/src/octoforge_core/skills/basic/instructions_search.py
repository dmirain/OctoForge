"""Basic skill searching the instructions store (knowledge/skills/tools)."""

from typing import Any

from octoforge_core.instructions.api import InstructionService, SearchHit
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "instructions_search"
SKILL_DESCRIPTION = (
    "Search the instructions store for relevant knowledge, skill scenarios and tools. "
    "Returns the top-k records with type, title, tags, relevance score and content snippet. "
    "Use it before non-trivial tasks to discover what is already known."
)
MAX_K = 20
SNIPPET_CHARS = 300
NO_HITS_MESSAGE = "no instructions found"
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for, in free text"},
        "k": {
            "type": "integer",
            "description": f"How many hits to return (1..{MAX_K})",
        },
    },
    "required": ["query"],
}


class InstructionsSearchSkill:
    """Thin adapter over the InstructionService facade."""

    def __init__(self, service: InstructionService, default_k: int) -> None:
        self._service = service
        self._default_k = default_k

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, search and format the hits as text."""
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SkillArgumentsError("query must be a non-empty string")
        k = self._parse_k(arguments.get("k"))
        hits = await self._service.search(query, k)
        if not hits:
            return NO_HITS_MESSAGE
        return "\n".join(_format_hit(index, hit) for index, hit in enumerate(hits, start=1))

    def _parse_k(self, raw: object) -> int:
        if raw is None:
            return self._default_k
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SkillArgumentsError("k must be an integer")
        if raw < 1 or raw > MAX_K:
            raise SkillArgumentsError(f"k must be between 1 and {MAX_K}")
        return raw


def _format_hit(index: int, hit: SearchHit) -> str:
    instruction = hit.instruction
    lines = [
        f"{index}. [{instruction.type.value}] {instruction.title} (score {hit.score:.3f})",
        f"   tags: {', '.join(instruction.tags) if instruction.tags else '-'}",
        f"   {_snippet(instruction.content)}",
    ]
    return "\n".join(lines)


def _snippet(content: str) -> str:
    one_line = content.replace("\n", " ")
    return one_line[:SNIPPET_CHARS]
