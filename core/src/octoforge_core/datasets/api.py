"""Public boundary of the datasets module.

Everything the rest of the system (tools, executors) may know about datasets
lives here: the `DatasetService` protocol, the `DatasetStore` storage port,
the JSON-serializable DTOs and the module errors.

The protocols are deliberately transport-shaped: DTOs contain only
JSON-compatible fields (datetimes serialize as ISO 8601 at a wire boundary),
so a future HTTP implementation of `DatasetService` is the planned "extract
to a dedicated service" path — call sites will not change.

The store port exists so an installer can swap only the persistence/vector
layer (e.g. pgvector or an external vector DB) without rewriting the service
orchestration (embedding, boosting, validation). Stores able to run the
vector search on their own side additionally implement the runtime-checkable
`DatasetVectorSearch` capability; the service detects it with isinstance and
stops pulling all of the owner's descriptors through `list_with_embeddings`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class FieldType(StrEnum):
    """Type of a dataset schema field."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class DatasetNotFoundError(Exception):
    """Raised when no dataset matches the requested (owner, name) pair."""


class DatasetExistsError(Exception):
    """Raised when the dataset name is already taken for this owner."""


class DatasetSchemaError(Exception):
    """Raised when a dataset schema fails to parse at creation time."""


class DatasetRecordValidationError(Exception):
    """Raised when a record payload violates the dataset schema."""

    def __init__(self, violations: tuple[str, ...] | list[str]) -> None:
        self.violations = tuple(violations)
        super().__init__("; ".join(self.violations))


@dataclass(frozen=True, slots=True)
class DatasetField:
    """One field of a dataset schema."""

    name: str
    type: FieldType
    required: bool


@dataclass(frozen=True, slots=True)
class DatasetSchema:
    """Schema of a dataset: an ordered set of named, typed fields."""

    fields: tuple[DatasetField, ...]


@dataclass(frozen=True, slots=True)
class Dataset:
    """One dataset descriptor (always owned by a single user).

    JSON-friendly: str/int fields, a nested schema of str/StrEnum/bool and
    UTC datetimes (ISO 8601 at a wire boundary). The embedding is intentionally
    not part of the DTO: it is a local implementation detail of the search
    engine.
    """

    id: str
    owner_user_id: str
    name: str
    description: str
    schema: DatasetSchema
    usage_notes: str
    retention: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    """One record of a dataset; payload is a JSON document (dict at the boundary)."""

    id: str
    dataset_id: str
    owner_user_id: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DatasetHit:
    """A dataset returned by search together with its relevance score."""

    dataset: Dataset
    score: float


@dataclass(frozen=True, slots=True)
class EmbeddedDataset:
    """A dataset descriptor together with its stored embedding (search input/output)."""

    dataset: Dataset
    embedding: tuple[float, ...]


class DatasetStore(Protocol):
    """Storage port of the datasets module: descriptors, records, embeddings.

    Implementation shipped with the core: `SqlAlchemyDatasetStore`
    (SQL, in-process). An installer substitutes its own (pgvector, an
    external vector DB) in the composition root without touching the service.
    `list_with_embeddings` is the brute-force path ("small data, rank in
    process"); stores that outgrow it should also implement
    `DatasetVectorSearch`. Owner isolation is the store's duty: every method
    is scoped by `owner_user_id` (or by a descriptor id resolved under one).
    """

    async def create(  # noqa: PLR0913, PLR0917 — flat row fields, mirroring the SQL rows
        self,
        owner_user_id: str,
        name: str,
        description: str,
        schema: DatasetSchema,
        usage_notes: str,
        retention: str,
        embedding: tuple[float, ...],
    ) -> Dataset:
        """Insert the descriptor; raise `DatasetExistsError` on (owner, name) races."""
        ...

    async def get(self, owner_user_id: str, name: str) -> Dataset | None:
        """Return the descriptor of this owner by name, or None."""
        ...

    async def add_record(
        self,
        dataset_id: str,
        owner_user_id: str,
        payload: dict[str, Any],
    ) -> DatasetRecord:
        """Append a record row to the dataset."""
        ...

    async def delete(self, owner_user_id: str, name: str) -> int | None:
        """Delete the descriptor with its records; return the record count or None."""
        ...

    async def list_with_embeddings(self, owner_user_id: str) -> list[EmbeddedDataset]:
        """Return every descriptor of this owner with its embedding (search input)."""
        ...

    async def query_candidates(
        self,
        dataset_id: str,
        date_from: datetime | None,
        date_to: datetime | None,
        scan_limit: int,
    ) -> list[DatasetRecord]:
        """Return records in the created_at range, newest first, capped at scan_limit."""
        ...


@runtime_checkable
class DatasetVectorSearch(Protocol):
    """Optional DatasetStore capability: vector search on the storage side.

    A store implementing this (e.g. pgvector) receives the query embedding
    and returns the owner's closest descriptors itself, so the service never
    pulls the whole table into the process. The service still applies its own
    exact-name boost over the returned candidates.
    """

    async def search_by_vector(
        self,
        owner_user_id: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[EmbeddedDataset]:
        """Return up to `limit` descriptors of this owner closest to the embedding."""
        ...


@runtime_checkable
class DatasetLexicalSearch(Protocol):
    """Optional DatasetStore capability: BM25 over descriptions.

    Deliberately narrower in value than its instructions counterpart, and worth
    saying so: an owner has a handful of descriptors, so the vector search over
    them was never slow. What this adds is the kind of match an embedding
    misses — an abbreviation, a code, a proper noun that appears in the
    description but is nowhere near it in vector space.
    """

    async def search_by_text(
        self,
        owner_user_id: str,
        query: str,
        limit: int,
    ) -> list[EmbeddedDataset]:
        """Return up to `limit` of this owner's descriptors matching the words.

        Descriptors sharing no term with the query are absent rather than
        ranked last, so a non-match casts no vote in the fusion.
        """
        ...


class DatasetService(Protocol):
    """Facade of the datasets module: store descriptors, records and search.

    Implementations: `LocalDatasetService` (SQL + cosine, in-process);
    a future HTTP client implementation for a dedicated datasets service.
    The implementation is chosen in the composition root.
    """

    async def create_dataset(  # noqa: PLR0913, PLR0917 — transport-shaped boundary signature
        self,
        owner_user_id: str,
        name: str,
        description: str,
        schema: DatasetSchema,
        usage_notes: str = "",
        retention: str = "",
    ) -> Dataset:
        """Create a dataset descriptor, embedding name + description + usage_notes.

        Raises `DatasetExistsError` when the name is already taken for this owner.
        """
        ...

    async def get_dataset(self, owner_user_id: str, name: str) -> Dataset:
        """Return the dataset of this owner by name; raise `DatasetNotFoundError`."""
        ...

    async def add_record(
        self,
        owner_user_id: str,
        dataset_name: str,
        payload: dict[str, Any],
    ) -> DatasetRecord:
        """Append a record to the owner's dataset.

        Owner isolation is enforced at the SQL level (WHERE owner_user_id):
        a dataset owned by someone else looks absent. Raises
        `DatasetNotFoundError` when no such dataset exists for this owner.
        Payload validation against the schema is the caller's duty — the
        service trusts it (the tools validate before calling).
        """
        ...

    async def query_records(  # noqa: PLR0913, PLR0917 — transport-shaped boundary signature
        self,
        owner_user_id: str,
        dataset_name: str,
        equals: dict[str, Any] | None,
        date_from: datetime | None,
        date_to: datetime | None,
        limit: int,
    ) -> list[DatasetRecord]:
        """Return matching records of the owner's dataset, newest first.

        `equals` filters payload fields by type-sensitive equality; the date
        range filters `created_at`. Raises `DatasetNotFoundError` when no such
        dataset exists for this owner.
        """
        ...

    async def delete_dataset(self, owner_user_id: str, name: str) -> int:
        """Delete the owner's dataset with all its records; return the record count.

        Raises `DatasetNotFoundError` when no such dataset exists for this owner.
        """
        ...

    async def search(self, owner_user_id: str, query: str, k: int) -> list[DatasetHit]:
        """Return the top-k dataset descriptors of this owner relevant to the query."""
        ...
