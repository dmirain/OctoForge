"""Tools of the instructions module: store search, authoring and deletion."""

from typing import Any

from octoforge_core.datasets.api import DatasetHit, DatasetService
from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
    SearchHit,
)
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

SEARCH_NAME = "recall"
SEARCH_DESCRIPTION = (
    "Search everything you know: skill scenarios (how tasks are done here), "
    "knowledge (shared facts), this user's private memories and their dataset "
    "descriptors — one ranked search over all of it, with no type taking more "
    "than half of the hits. "
    "Pass 'type' to search one record kind only: type=memory for the user's "
    "memories alone, type=endpoint to discover external API endpoints (they are "
    "kept out of the default results — skills name the endpoints they use, and "
    "endpoint_get resolves a named endpoint's contract). "
    "Returns the top-k records with id, type, title, tags and full content: "
    "instructions first, dataset descriptors after them. "
    "This is the FIRST tool to call for any request that is not small talk, before "
    "you plan an approach or touch any other tool: the store usually already holds "
    "the scenario or the facts, and following a stored record beats designing your "
    "own way. It is a cheap local lookup — the cost of a redundant search is far "
    "below the cost of improvising past an existing one. Query with the intent "
    "plus the entity it concerns ('remind reminder', 'report user-data')."
)
MAX_K = 20
DATASET_SNIPPET_CHARS = 300
MAX_OUTPUT_CHARS = 8000
NO_HITS_MESSAGE = "no matching instructions or datasets"
TRUNCATED_MESSAGE = (
    "... output truncated: {omitted} more hit(s) not shown — refine the query or lower k"
)
SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What to look for, in free text"},
        "k": {
            "type": "integer",
            "description": f"How many hits to return (1..{MAX_K})",
        },
        "type": {
            "type": "string",
            "enum": [kind.value for kind in InstructionType],
            "description": (
                "Optional record kind filter: knowledge, skill, memory or endpoint "
                "(endpoints appear only with this filter)"
            ),
        },
    },
    "required": ["query"],
}

SAVE_NAME = "instruction_save"
SAVE_DESCRIPTION = (
    "Create or update one of your instructions: "
    "knowledge — a durable fact useful to everyone (saved as your private record; "
    "an admin can publish it later), "
    "skill — a how-to scenario for a task that is not solvable upfront "
    "(e.g. to get the weather, call the weather endpoint via external_call), "
    "endpoint — an API request contract executed by external_call (like an MCP tool). "
    "The record belongs to you: only you can see and delete it. "
    "Existing (type, title) records of yours are replaced with a bumped version. "
    "If one of your records was published, you stay its author: saving the same "
    "(type, title) updates the published record for everyone. "
    "Personal facts about the user are not instructions: save them with memory_store."
)
SAVED_TEMPLATE = "instruction saved: [{kind}] {title} (version {version})"
# memory records are written through memory_store, never through this tool
SAVABLE_KINDS = tuple(kind for kind in InstructionType if kind is not InstructionType.MEMORY)
SAVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [kind.value for kind in SAVABLE_KINDS],
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

DELETE_NAME = "instruction_delete"
DELETE_DESCRIPTION = (
    "Delete one of your instructions by its id (ids come from recall "
    "results). Only your own records can be deleted."
)
DELETED_MESSAGE = "instruction deleted"
NOT_FOUND_MESSAGE = "instruction not found (only your own records can be deleted)"
DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Instruction id from recall results"},
    },
    "required": ["id"],
}


class InstructionSearchTool:
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
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=SEARCH_NAME,
            description=SEARCH_DESCRIPTION,
            parameters_schema=SEARCH_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, search both stores and format the hits as text.

        Instructions come first (in the service's ranking order), dataset
        descriptors after them — the two rankings use incomparable score
        scales, so the blocks are never merged by score. The merged list is
        capped at k entries and the whole output at MAX_OUTPUT_CHARS, with a
        truncation note when the list is cut short.
        """
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentsError("query must be a non-empty string")
        k = self._parse_k(arguments.get("k"))
        kind = _parse_optional_kind(arguments.get("type"))
        hits = await self._service.search(context.user_id, query, k, kind)
        bodies = [_instruction_body(hit) for hit in hits]
        if self._datasets is not None:
            dataset_hits = await self._datasets.search(context.user_id, query, k)
            bodies.extend(_dataset_body(hit) for hit in dataset_hits)
        if not bodies:
            return NO_HITS_MESSAGE
        return _render(bodies[:k])

    def _parse_k(self, raw: object) -> int:
        if raw is None:
            return self._default_k
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ToolArgumentsError("k must be an integer")
        if raw < 1 or raw > MAX_K:
            raise ToolArgumentsError(f"k must be between 1 and {MAX_K}")
        return raw


class InstructionSaveTool:
    """Thin adapter over the InstructionService facade."""

    def __init__(self, service: InstructionService) -> None:
        self._service = service

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=SAVE_NAME,
            description=SAVE_DESCRIPTION,
            parameters_schema=SAVE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, upsert the caller's record and confirm the version."""
        kind = _parse_kind(arguments.get("type"))
        if kind is InstructionType.MEMORY:
            raise ToolArgumentsError("personal memories are saved with memory_store, not here")
        title = arguments.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ToolArgumentsError("title must be a non-empty string")
        content = arguments.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ToolArgumentsError("content must be a non-empty string")
        tags = _parse_tags(arguments.get("tags"))
        instruction = await self._service.save(context.user_id, kind, title, content, tags)
        return SAVED_TEMPLATE.format(
            kind=instruction.type.value,
            title=instruction.title,
            version=instruction.version,
        )


class InstructionDeleteTool:
    """Deletes the caller's own instruction by id (ids come from search results)."""

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
        """Validate the id and delete the record when it belongs to the caller."""
        instruction_id = arguments.get("id")
        if not isinstance(instruction_id, str) or not instruction_id.strip():
            raise ToolArgumentsError("id must be a non-empty string")
        try:
            await self._service.delete(context.user_id, instruction_id)
        except InstructionNotFoundError:
            return NOT_FOUND_MESSAGE
        return DELETED_MESSAGE


def _render(bodies: list[str]) -> str:
    """Number the bodies and join them, capping the whole output at MAX_OUTPUT_CHARS.

    The first body is always emitted whole; further bodies that would cross the
    cap are dropped and replaced with a truncation note so the model knows the
    list is partial.
    """
    lines: list[str] = []
    length = 0
    omitted = 0
    for index, body in enumerate(bodies, start=1):
        numbered = f"{index}. {body}"
        if lines and length + len(numbered) + 1 > MAX_OUTPUT_CHARS:
            omitted = len(bodies) - index + 1
            break
        lines.append(numbered)
        length += len(numbered) + 1
    if omitted:
        lines.append(TRUNCATED_MESSAGE.format(omitted=omitted))
    return "\n".join(lines)


def _instruction_body(hit: SearchHit) -> str:
    """Format an instruction hit with the FULL content (scenarios are read in place)."""
    instruction = hit.instruction
    return "\n".join(
        [
            f"[{instruction.type.value}] {instruction.title}",
            f"   id: {instruction.id}",
            f"   tags: {', '.join(instruction.tags) if instruction.tags else '-'}",
            instruction.content,
        ]
    )


def _dataset_body(hit: DatasetHit) -> str:
    dataset = hit.dataset
    field_names = ", ".join(field.name for field in dataset.schema.fields)
    return "\n".join(
        [
            f"[dataset] {dataset.name}",
            f"   fields: {field_names or '-'}",
            f"   {_snippet(dataset.description)}",
        ]
    )


def _snippet(content: str) -> str:
    """One-line snippet for dataset descriptors (scenarios themselves go in full)."""
    one_line = content.replace("\n", " ")
    return one_line[:DATASET_SNIPPET_CHARS]


def _parse_kind(raw: object) -> InstructionType:
    try:
        return InstructionType(str(raw))
    except ValueError as exc:
        raise ToolArgumentsError(f"unsupported instruction type: {raw!r}") from exc


def _parse_optional_kind(raw: object) -> InstructionType | None:
    if raw is None:
        return None
    return _parse_kind(raw)


def _parse_tags(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, list) and all(isinstance(tag, str) for tag in raw):
        return tuple(raw)
    raise ToolArgumentsError("tags must be an array of strings")
