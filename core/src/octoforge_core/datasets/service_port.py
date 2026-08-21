"""Public service interface of the datasets module."""

from typing import Any, Protocol

from octoforge_core.datasets.requests import DatasetDefinition, DatasetRecordQuery
from octoforge_core.datasets.types import Dataset, DatasetHit, DatasetRecord


class DatasetService(Protocol):
    """Store dataset descriptors and records, then retrieve them by query or meaning."""

    async def create_dataset(self, definition: DatasetDefinition) -> Dataset:
        """Create and embed one dataset descriptor."""
        ...

    async def get_dataset(self, owner_user_id: str, name: str) -> Dataset:
        """Return this owner's named dataset or raise when absent."""
        ...

    async def add_record(
        self,
        owner_user_id: str,
        dataset_name: str,
        payload: dict[str, Any],
    ) -> DatasetRecord:
        """Append a record to this owner's named dataset."""
        ...

    async def query_records(self, request: DatasetRecordQuery) -> list[DatasetRecord]:
        """Return owner-scoped records matching the request, newest first."""
        ...

    async def delete_dataset(self, owner_user_id: str, name: str) -> int:
        """Delete this owner's named dataset and return its record count."""
        ...

    async def search(self, owner_user_id: str, query: str, k: int) -> list[DatasetHit]:
        """Return the top-k descriptors relevant to the query."""
        ...
