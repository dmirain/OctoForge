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
    """Kind of an instruction record.

    MEMORY is the per-user slice of the same store: a memory is structurally a
    private knowledge record (title = the memory key), so it shares the table,
    the embeddings and the search machinery instead of maintaining a parallel
    LIKE-searched store. Memory records are always owned and never published.
    """

    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    ENDPOINT = "endpoint"
    MEMORY = "memory"


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
    startup sync only, never by agent-facing save/delete. `owner_id` is the
    record's author (set from the caller's session); None marks a public
    record visible to everyone — new records are private and only the
    admin-facing `publish` makes them public.
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
    owner_id: str | None = None


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
    """Upsert input of the store: record fields, the embedding and the ownership.

    `owner_id` None creates/updates the public (or system) record identified
    by (kind, title); a user id scopes the upsert to that owner's private
    record, so two users may hold private records with the same title.
    """

    kind: InstructionType
    title: str
    content: str
    tags: tuple[str, ...]
    embedding: tuple[float, ...]
    system: bool = False
    owner_id: str | None = None


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

        The upsert target is (kind, title, owner_id). The update path also
        rewrites the `system` flag (registry adoption); the agent-facing
        protection of system records lives in the service.
        """
        ...

    async def get_by_title(
        self,
        title: str,
        kind: InstructionType | None,
        owner_id: str | None = None,
    ) -> Instruction | None:
        """Return the record by (title, kind, owner), oldest first on collisions.

        `owner_id` None targets the public record; private lookups pass the
        owning user id. Returns None when nothing matches.
        """
        ...

    async def list_with_embeddings(self, user_id: str | None) -> list[EmbeddedInstruction]:
        """Return records visible to `user_id` with their embeddings.

        Visibility: public records plus the user's own private ones.
        `user_id` None returns the whole table (internal/admin surface).
        """
        ...

    async def list_system(self) -> list[Instruction]:
        """Return every system (registry-owned) record, oldest first."""
        ...

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        """Increment usage_count of the given records (search hits proved useful)."""
        ...

    async def delete_by_id(self, instruction_id: str, owner_id: str) -> bool:
        """Delete the owner's private record by id; return True when removed.

        Public records (owner_id NULL) never match: publishing and deleting
        them is an admin surface, not this owner-scoped path.
        """
        ...

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        """Delete the public/system record (kind, title); registry sync only.

        Never matches private records. Agent-facing deletion goes through
        `delete_by_id` instead.
        """
        ...

    async def publish(self, instruction_id: str) -> Instruction | None:
        """Make the record public (owner_id -> NULL); None when the id is unknown.

        Memory-type records also answer None: a user's memory is never
        publishable, and to the admin surface an unpublishable record looks
        the same as a missing one.
        """
        ...

    async def list_missing_embeddings(self) -> list[Instruction]:
        """Return records stored with an empty embedding (deferred embedding).

        Two sources produce them: a save whose embedding call failed (the
        fact is kept rather than lost) and data migrations, which run without
        an embedder. The startup sweep re-embeds them.
        """
        ...

    async def set_embedding(self, instruction_id: str, embedding: tuple[float, ...]) -> bool:
        """Store the embedding without touching content, version or timestamps.

        Returns False when the id is unknown (the record was deleted between
        the listing and the sweep).
        """
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
        user_id: str | None,
    ) -> list[EmbeddedInstruction]:
        """Return up to `limit` visible records closest to the query, best first.

        Same visibility rule as `list_with_embeddings`: public plus the user's
        own records; `user_id` None searches the whole table.
        """
        ...


class InstructionService(Protocol):
    """Facade of the instructions module: store, search and rank.

    Implementations: `LocalInstructionService` (SQL + cosine, in-process);
    a future HTTP client implementation for a dedicated instructions service.
    The implementation is chosen in the composition root.
    """

    async def search(
        self,
        user_id: str,
        query: str,
        k: int,
        kind: InstructionType | None = None,
    ) -> list[SearchHit]:
        """Return the top-k instructions relevant to the query, best first.

        Only records visible to `user_id` (public plus their own) take part;
        `kind` narrows the search to one instruction type before ranking.
        Documented side effect: implementations bump `usage_count` of the
        returned hits — search is the moment an instruction proves useful.
        """
        ...

    async def search_all(
        self,
        query: str,
        k: int,
        kind: InstructionType | None = None,
    ) -> list[SearchHit]:
        """Search the whole table with no visibility filter (admin surface).

        Not for agent-facing tools: the admin console uses it to discover
        private records by their ids before publishing them. Memory records
        are excluded unless `kind` names them explicitly — a cross-user
        search must not surface personal memories casually.
        """
        ...

    async def save(
        self,
        user_id: str,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Create or replace the caller's private record identified by (kind, title).

        The owner always comes from the caller's session, never from tool
        arguments. A public record with the same (kind, title) is left alone —
        the save creates a private copy shadowing it for the owner. An existing
        private record gets its content/tags replaced, its version bumped and
        its embedding recomputed; usage/success counters survive.
        Raises `SystemInstructionError` when a system record holds the title.
        """
        ...

    async def get_by_name(
        self,
        name: str,
        kind: InstructionType | None = None,
        user_id: str | None = None,
    ) -> Instruction:
        """Return the instruction titled `name`, optionally narrowed by type.

        With `user_id` given, only records visible to that user take part
        (their own plus public); without it, only public records. When several
        candidates share the title, the oldest record wins. Raises
        `InstructionNotFoundError` when nothing matches.
        """
        ...

    async def delete(self, user_id: str, instruction_id: str) -> None:
        """Delete the caller's private record by id.

        Raises `InstructionNotFoundError` when no own record matches the id —
        a public or someone else's record looks the same as a missing one to
        the agent-facing caller (publishing is the admin surface).
        """
        ...

    async def publish(self, instruction_id: str) -> Instruction:
        """Make the record public (owner -> None); admin surface, not an agent tool.

        Raises `InstructionNotFoundError` when the id is unknown — or when it
        names a memory record: a user's memory is never publishable.
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

    async def reembed_missing(self) -> int:
        """Embed and store vectors for records saved without one; return the count.

        Called from the composition root at startup: it finishes what a
        failed embedding backend or an embedder-less data migration left
        behind. A record that fails again simply stays in the queue for the
        next sweep.
        """
        ...
