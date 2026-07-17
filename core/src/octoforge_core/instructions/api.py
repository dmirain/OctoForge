"""Public boundary of the instructions module.

Everything the rest of the system (agent loop, skills, executors) may know
about instructions lives here: the `InstructionService` protocol, the
JSON-serializable DTOs and the module errors.

The protocol is deliberately transport-shaped: DTOs contain only
JSON-compatible fields (datetimes serialize as ISO 8601 at a wire boundary),
so a future HTTP implementation of `InstructionService` is the planned
"extract to a dedicated service" path — call sites will not change.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class InstructionType(StrEnum):
    """Kind of an instruction record."""

    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    TOOL = "tool"


class InstructionNotFoundError(Exception):
    """Raised when no instruction matches the requested name."""


@dataclass(frozen=True, slots=True)
class Instruction:
    """One instruction record (knowledge, skill scenario or tool description).

    JSON-friendly: str/int/float fields, a tuple of str tags, an StrEnum type
    (serializes as its value) and UTC datetimes (ISO 8601 at a wire boundary).
    The embedding is intentionally not part of the DTO: it is a local
    implementation detail of the search engine.
    """

    id: str
    type: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]
    version: int
    usage_count: int
    success_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SearchHit:
    """An instruction returned by search together with its relevance score."""

    instruction: Instruction
    score: float


class InstructionService(Protocol):
    """Facade of the instructions module: store, search and rank.

    Implementations: `LocalInstructionService` (SQL + cosine, in-process);
    a future HTTP client implementation for a dedicated instructions service.
    The implementation is chosen in the composition root.
    """

    async def search(self, query: str, k: int) -> list[SearchHit]:
        """Return the top-k instructions relevant to the query, best first.

        Documented side effect: implementations bump `usage_count` of the
        returned hits — search is the moment an instruction proves useful.
        """
        ...

    async def save(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Create or replace the instruction identified by (kind, title).

        Upsert: an existing record gets its content/tags replaced, its version
        bumped and its embedding recomputed; usage/success counters survive.
        """
        ...

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
        """Return the instruction titled `name`, optionally narrowed by type.

        When several types share the title and `kind` is None, the oldest
        record wins. Raises `InstructionNotFoundError` when nothing matches.
        """
        ...
