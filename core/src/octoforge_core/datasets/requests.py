"""Typed requests for dataset creation, record scans, queries and ranking."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from octoforge_core.datasets.types import DatasetSchema, EmbeddedDataset


@dataclass(frozen=True, slots=True)
class DatasetDefinition:
    """Everything supplied by a caller when creating one dataset."""

    owner_user_id: str
    name: str
    description: str
    schema: DatasetSchema
    usage_notes: str = ""
    retention: str = ""


@dataclass(frozen=True, slots=True)
class DatasetRecordQuery:
    """Owner-scoped filters and result size for a record query."""

    owner_user_id: str
    dataset_name: str
    equals: dict[str, Any] | None
    date_from: datetime | None
    date_to: datetime | None
    limit: int


@dataclass(frozen=True, slots=True)
class DatasetRecordScan:
    """Storage-side date scan before payload equality filtering."""

    dataset_id: str
    date_from: datetime | None
    date_to: datetime | None
    limit: int


@dataclass(frozen=True, slots=True)
class DatasetRankingRequest:
    """Complete input to deterministic cosine dataset ranking."""

    candidates: list[EmbeddedDataset]
    query: str
    query_embedding: tuple[float, ...]
    limit: int
