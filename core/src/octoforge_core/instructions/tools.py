"""Tools of the instructions module: store search, authoring and deletion."""

import asyncio
from typing import Any

from octoforge_core.datasets.api import DatasetHit, DatasetService
from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
    SearchHit,
)
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
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
# What the model needs to write a WORKING endpoint record. It is in the tool
# description, not a skill, because a record is authored blind: the contract
# it produces is only exercised later, and until 2026-08-05 the failures were
# silent (invented field names, a path escaped into %2F, a host it could not
# express). Each line below is one of those failures.
ENDPOINT_CONTRACT_HINT = (
    "An endpoint's content is a JSON contract with these fields and no others "
    "(unknown ones are refused): method, url_template, params_schema, headers, "
    "body_template, auth (plus free-form notes/description). "
    "Placeholders work in url_template, body_template and header values: "
    "{param} is declared in params_schema, {user.code} is a per-user value an "
    "operator set, {secret.code} is a per-user secret — you see the name, never "
    'the value. Declare a secret as auth {"secret": "<code>", "format": '
    '"Bearer {value}"} or write {secret.<code>} into a header; auth "basic"/'
    '"bearer" as a bare word attaches NOTHING. params_schema types: "string" — '
    'one path segment, slashes escaped; "path" — several segments, slashes '
    'kept (what a CalDAV/discovery href needs); "host" — a hostname, requires '
    '"hosts": ["*.example.com"] and is the only type allowed where the URL\'s '
    "host stands, which is how a contract follows a service across sibling "
    "hosts instead of hard-coding one user's shard. Check secret_list for the "
    "codes a user actually has."
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
        "content": {
            "type": "string",
            "description": f"Instruction body. {ENDPOINT_CONTRACT_HINT}",
        },
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
        # both stores answer the same question about different tables, so the
        # dataset lookup has no reason to wait out the instruction search's
        # round trips; the blocks are still rendered instructions-first
        hits, dataset_hits = await asyncio.gather(
            self._service.search(context.user_id, query, k, kind),
            self._dataset_hits(context.user_id, query, k),
        )
        bodies = [_instruction_body(hit) for hit in hits]
        bodies.extend(_dataset_body(hit) for hit in dataset_hits)
        if not bodies:
            return NO_HITS_MESSAGE
        return _render(bodies[:k])

    async def _dataset_hits(self, user_id: str, query: str, k: int) -> list[DatasetHit]:
        """Dataset descriptors, or nothing when this deployment has no datasets."""
        if self._datasets is None:
            return []
        return await self._datasets.search(user_id, query, k)

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
        # gated per kind, not per tool: the same tool saves knowledge, which
        # every plan keeps
        if kind is InstructionType.SKILL and not feature_enabled(
            context.enabled_features, FeatureCode.SKILL_CREATE
        ):
            return feature_refusal(FeatureCode.SKILL_CREATE)
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
