"""Persistence and optional search capabilities of the datasets module."""

from typing import Any, Protocol, runtime_checkable

from octoforge_core.datasets.requests import DatasetDefinition, DatasetRecordScan
from octoforge_core.datasets.types import Dataset, DatasetRecord, EmbeddedDataset


class DatasetStore(Protocol):
    """Owner-scoped persistence for dataset descriptors, records and embeddings."""

    async def create(
        self,
        definition: DatasetDefinition,
        embedding: tuple[float, ...],
    ) -> Dataset:
        """Insert a descriptor; raise on an owner/name uniqueness race."""
        ...

    async def get(self, owner_user_id: str, name: str) -> Dataset | None:
        """Return this owner's descriptor by name, or None."""
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
        """Delete the descriptor and records; return the record count or None."""
        ...

    async def list_with_embeddings(self, owner_user_id: str) -> list[EmbeddedDataset]:
        """Return this owner's descriptors with embeddings."""
        ...

    async def query_candidates(self, request: DatasetRecordScan) -> list[DatasetRecord]:
        """Return records in the requested date range, newest first."""
        ...


@runtime_checkable
class DatasetVectorSearch(Protocol):
    """Optional store capability for vector search on the storage side."""

    async def search_by_vector(
        self,
        owner_user_id: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[EmbeddedDataset]:
        """Return this owner's nearest descriptors."""
        ...


@runtime_checkable
class DatasetLexicalSearch(Protocol):
    """Optional store capability for lexical descriptor search."""

    async def search_by_text(
        self,
        owner_user_id: str,
        query: str,
        limit: int,
    ) -> list[EmbeddedDataset]:
        """Return this owner's descriptors matching the query words."""
        ...
