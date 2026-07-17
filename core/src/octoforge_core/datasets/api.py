"""Public boundary of the datasets module.

Everything the rest of the system (skills, executors) may know about datasets
lives here: the `DatasetService` protocol, the JSON-serializable DTOs and the
module errors.

The protocol is deliberately transport-shaped: DTOs contain only
JSON-compatible fields (datetimes serialize as ISO 8601 at a wire boundary),
so a future HTTP implementation of `DatasetService` is the planned "extract
to a dedicated service" path — call sites will not change.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol


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


class DatasetService(Protocol):
    """Facade of the datasets module: store descriptors, records and search.

    Implementations: `LocalDatasetService` (SQL + cosine, in-process);
    a future HTTP client implementation for a dedicated datasets service.
    The implementation is chosen in the composition root.
    """

    async def create_dataset(  # noqa: PLR0913 — transport-shaped boundary signature
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
        service trusts it (the skills validate before calling).
        """
        ...

    async def query_records(  # noqa: PLR0913 — transport-shaped boundary signature
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
