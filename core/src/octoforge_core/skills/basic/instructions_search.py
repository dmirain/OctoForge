"""Basic skill searching the instructions store (knowledge/skills/tools) and user datasets."""

from dataclasses import dataclass
from typing import Any

from octoforge_core.datasets.api import DatasetHit, DatasetService
from octoforge_core.instructions.api import InstructionService, SearchHit
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "instructions_search"
SKILL_DESCRIPTION = (
    "Search the instructions store and the user's datasets for relevant knowledge, "
    "skill scenarios, tools and dataset descriptors. "
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


@dataclass(frozen=True, slots=True)
class _MergedEntry:
    """One preformatted hit of either kind, merged and numbered after sorting."""

    score: float
    key: str
    body: str


class InstructionsSearchSkill:
    """Thin adapter over the InstructionService facade (and, optionally, DatasetService)."""

    def __init__(
        self,
        service: InstructionService,
        default_k: int,
        datasets: DatasetService | None = None,
    ) -> None:
        self._service = service
        self._default_k = default_k
        self._datasets = datasets

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, search both stores and format the merged hits as text."""
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SkillArgumentsError("query must be a non-empty string")
        k = self._parse_k(arguments.get("k"))
        hits = await self._service.search(query, k)
        entries = [_instruction_entry(hit) for hit in hits]
        if self._datasets is not None:
            dataset_hits = await self._datasets.search(context.user_id, query, k)
            entries.extend(_dataset_entry(hit) for hit in dataset_hits)
        if not entries:
            return NO_HITS_MESSAGE
        entries.sort(key=lambda entry: (-entry.score, entry.key))
        return "\n".join(f"{index}. {entry.body}" for index, entry in enumerate(entries, start=1))

    def _parse_k(self, raw: object) -> int:
        if raw is None:
            return self._default_k
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SkillArgumentsError("k must be an integer")
        if raw < 1 or raw > MAX_K:
            raise SkillArgumentsError(f"k must be between 1 and {MAX_K}")
        return raw


def _instruction_entry(hit: SearchHit) -> _MergedEntry:
    instruction = hit.instruction
    body = "\n".join(
        [
            f"[{instruction.type.value}] {instruction.title} (score {hit.score:.3f})",
            f"   tags: {', '.join(instruction.tags) if instruction.tags else '-'}",
            f"   {_snippet(instruction.content)}",
        ]
    )
    return _MergedEntry(score=hit.score, key=instruction.title, body=body)


def _dataset_entry(hit: DatasetHit) -> _MergedEntry:
    dataset = hit.dataset
    field_names = ", ".join(field.name for field in dataset.schema.fields)
    body = "\n".join(
        [
            f"[dataset] {dataset.name} (score {hit.score:.3f})",
            f"   fields: {field_names or '-'}",
            f"   {_snippet(dataset.description)}",
        ]
    )
    return _MergedEntry(score=hit.score, key=dataset.name, body=body)


def _snippet(content: str) -> str:
    one_line = content.replace("\n", " ")
    return one_line[:SNIPPET_CHARS]
