"""Tools of the instructions module: store search and scenario authoring."""

from dataclasses import dataclass
from typing import Any

from octoforge_core.datasets.api import DatasetHit, DatasetService
from octoforge_core.instructions.api import InstructionService, InstructionType, SearchHit
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SEARCH_NAME = "skills_search"
SEARCH_DESCRIPTION = (
    "Search the instructions store and the user's datasets for relevant skill "
    "scenarios, knowledge, endpoints and dataset descriptors. "
    "Returns the top-k records with type, title, tags, relevance score and full content. "
    "Call it for any user intent not covered by the scenarios already in your context."
)
MAX_K = 20
DATASET_SNIPPET_CHARS = 300
NO_HITS_MESSAGE = "no skills found"
SEARCH_SCHEMA: dict[str, Any] = {
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

SAVE_NAME = "instruction_save"
SAVE_DESCRIPTION = (
    "Create or update an instruction in the store: a durable fact (knowledge), "
    "an action scenario (skill) or an endpoint description (endpoint). "
    "Existing (type, title) records are replaced with a bumped version."
)
SAVED_TEMPLATE = "instruction saved: [{kind}] {title} (version {version})"
SAVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [kind.value for kind in InstructionType],
            "description": "Instruction kind",
        },
        "title": {"type": "string", "description": "Unique (per type) instruction title"},
        "content": {"type": "string", "description": "Instruction body"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional tags for searchability",
        },
    },
    "required": ["type", "title", "content"],
}


@dataclass(frozen=True, slots=True)
class _MergedEntry:
    """One preformatted hit of either kind, merged and numbered after sorting."""

    score: float
    key: str
    body: str


class SkillsSearchSkill:
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
            name=SEARCH_NAME,
            description=SEARCH_DESCRIPTION,
            parameters_schema=SEARCH_SCHEMA,
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


class InstructionSaveSkill:
    """Thin adapter over the InstructionService facade."""

    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SAVE_NAME,
            description=SAVE_DESCRIPTION,
            parameters_schema=SAVE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, upsert the instruction and confirm with the version."""
        kind = _parse_kind(arguments.get("type"))
        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SkillArgumentsError("title must be a non-empty string")
        content = arguments.get("content")
        if not isinstance(content, str) or not content:
            raise SkillArgumentsError("content must be a non-empty string")
        tags = _parse_tags(arguments.get("tags"))
        instruction = await self._service.save(kind, title, content, tags)
        return SAVED_TEMPLATE.format(
            kind=instruction.type.value,
            title=instruction.title,
            version=instruction.version,
        )


def _instruction_entry(hit: SearchHit) -> _MergedEntry:
    """Format an instruction hit with the FULL content (scenarios are read in place)."""
    instruction = hit.instruction
    body = "\n".join(
        [
            f"[{instruction.type.value}] {instruction.title} (score {hit.score:.3f})",
            f"   tags: {', '.join(instruction.tags) if instruction.tags else '-'}",
            instruction.content,
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
    """One-line snippet for dataset descriptors (scenarios themselves go in full)."""
    one_line = content.replace("\n", " ")
    return one_line[:DATASET_SNIPPET_CHARS]


def _parse_kind(raw: object) -> InstructionType:
    try:
        return InstructionType(str(raw))
    except ValueError as exc:
        raise SkillArgumentsError(f"unsupported instruction type: {raw!r}") from exc


def _parse_tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, list) and all(isinstance(tag, str) for tag in raw):
        return tuple(raw)
    raise SkillArgumentsError("tags must be an array of strings")
