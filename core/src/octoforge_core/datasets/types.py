"""Dataset values and errors shared across the module boundary."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class FieldType(StrEnum):
    """Type of a dataset schema field."""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"


class DatasetNotFoundError(Exception):
    """Raised when no dataset matches the requested owner and name."""


class DatasetExistsError(Exception):
    """Raised when the dataset name is already taken for this owner."""


class DatasetSchemaError(Exception):
    """Raised when a dataset schema fails to parse at creation time."""


class DatasetQuotaError(Exception):
    """Raised when creating a dataset would exceed the owner's plan cap."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            f"the plan allows at most {limit} datasets; delete one first (data_forget)"
        )


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
    """One dataset descriptor, always owned by a single user."""

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
    """One record of a dataset; payload is a JSON document."""

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
    """A dataset descriptor together with its stored embedding."""

    dataset: Dataset
    embedding: tuple[float, ...]
