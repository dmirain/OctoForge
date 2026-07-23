"""Public boundary of the instructions module.

Everything the rest of the system (agent loop, tools, executors) may know
about instructions lives here: the `InstructionService` protocol, the
`InstructionStore` storage port, the JSON-serializable DTOs and the module
errors.

The protocols are deliberately transport-shaped: DTOs contain only
JSON-compatible fields (datetimes serialize as ISO 8601 at a wire boundary),
so a future HTTP implementation of `InstructionService` is the planned
"extract to a dedicated service" path — call sites will not change.

The store port exists so an installer can swap only the persistence/vector
layer (e.g. pgvector or an external vector DB) without rewriting the service
orchestration (embedding, boosting, reranking). Stores able to run the
vector search on their own side additionally implement the runtime-checkable
`InstructionVectorSearch` capability; the service detects it with isinstance
and stops pulling the whole table through `list_with_embeddings`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class InstructionType(StrEnum):
    """Kind of an instruction record."""

    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    ENDPOINT = "endpoint"


class InstructionNotFoundError(Exception):
    """Raised when no instruction matches the requested name."""


class SystemInstructionError(Exception):
    """Raised when an agent-facing write targets a system (registry-owned) record."""


@dataclass(frozen=True, slots=True)
class Instruction:
    """One instruction record (knowledge, skill scenario or endpoint description).

    JSON-friendly: str/int/float fields, a tuple of str tags, an StrEnum type
    (serializes as its value) and UTC datetimes (ISO 8601 at a wire boundary).
    The embedding is intentionally not part of the DTO: it is a local
    implementation detail of the search engine. `system` marks records owned
    by the declarative system registry: they are upserted/deleted by the
    startup sync only, never by agent-facing save/delete.
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
    system: bool = False


@dataclass(frozen=True, slots=True)
class SearchHit:
    """An instruction returned by search together with its relevance score."""

    instruction: Instruction
    score: float


@dataclass(frozen=True, slots=True)
class EmbeddedInstruction:
    """An instruction together with its stored embedding (search input/output)."""

    instruction: Instruction
    embedding: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class InstructionDraft:
    """Upsert input of the store: record fields, the embedding and the ownership flag."""

    kind: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]
    embedding: tuple[float, ...]
    system: bool = False


class InstructionStore(Protocol):
    """Storage port of the instructions module: records plus their embeddings.

    Implementation shipped with the core: `SqlAlchemyInstructionStore`
    (SQL, in-process). An installer substitutes its own (pgvector, an
    external vector DB) in the composition root without touching the service.
    `list_with_embeddings` is the brute-force path ("small data, rank in
    process"); stores that outgrow it should also implement
    `InstructionVectorSearch`.
    """

    async def upsert(self, draft: InstructionDraft) -> Instruction:
        """Create the record or replace content/tags/embedding, bumping the version.

        The update path also rewrites the `system` flag (registry adoption);
        the agent-facing protection of system records lives in the service.
        """
        ...

    async def get_by_title(self, title: str, kind: InstructionType | None) -> Instruction | None:
        """Return the record by title (oldest first when types collide), or None."""
        ...

    async def list_with_embeddings(self) -> list[EmbeddedInstruction]:
        """Return every record with its embedding (brute-force search input)."""
        ...

    async def list_system(self) -> list[Instruction]:
        """Return every system (registry-owned) record, oldest first."""
        ...

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        """Increment usage_count of the given records (search hits proved useful)."""
        ...

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        """Delete the record identified by (kind, title); return True when removed."""
        ...


@runtime_checkable
class InstructionVectorSearch(Protocol):
    """Optional InstructionStore capability: vector search on the storage side.

    A store implementing this (e.g. pgvector) receives the query embedding
    and returns the closest records itself, so the service never pulls the
    whole table into the process. The service still applies its own exact
    -title boost and reranker over the returned candidates.
    """

    async def search_by_vector(
        self,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[EmbeddedInstruction]:
        """Return up to `limit` records closest to the query embedding, best first."""
        ...


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
        Raises `SystemInstructionError` when the existing record is system.
        """
        ...

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
        """Return the instruction titled `name`, optionally narrowed by type.

        When several types share the title and `kind` is None, the oldest
        record wins. Raises `InstructionNotFoundError` when nothing matches.
        """
        ...

    async def delete(self, name: str, kind: InstructionType) -> None:
        """Delete the instruction identified by (kind, title).

        Raises `InstructionNotFoundError` when nothing matches and
        `SystemInstructionError` when the record is system.
        """
        ...

    async def save_system(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Create or replace a system (registry-owned) record.

        Management surface for the composition root's system-registry sync,
        not for agent-facing tools: a (kind, title) match is adopted — its
        content/tags/embedding are replaced and the record becomes system.
        An already-system record with unchanged content and tags is returned
        as-is (no re-embedding, no version bump).
        """
        ...

    async def list_system(self) -> list[Instruction]:
        """Return every system (registry-owned) record."""
        ...

    async def delete_system(self, name: str, kind: InstructionType) -> None:
        """Delete a record regardless of the system flag (registry sync only).

        Raises `InstructionNotFoundError` when nothing matches.
        """
        ...
