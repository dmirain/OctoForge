"""Local in-process implementation of the DatasetService facade.

The storage layer is the `DatasetStore` port injected through the
constructor: the shipped `SqlAlchemyDatasetStore` ranks brute-force in the
process, an installer can substitute a pgvector/vector-DB store
(implementing `DatasetVectorSearch`) without touching this service.
"""

from typing import Any

from octoforge_core.datasets._authoring import DatasetAuthor
from octoforge_core.datasets._search import DatasetSearch
from octoforge_core.datasets.requests import (
    DatasetDefinition,
    DatasetRecordQuery,
    DatasetRecordScan,
)
from octoforge_core.datasets.store_ports import DatasetStore
from octoforge_core.datasets.types import Dataset, DatasetHit, DatasetNotFoundError, DatasetRecord
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.tariffs.api import LimitGate

# Upper bound of rows scanned per query: the store filters the created_at
# range and caps the scan, the equals filter then applies in Python (tracker
# datasets are small, so no JSON-field indexes are needed).
MAX_SCAN_ROWS = 1000


class LocalDatasetService:
    """DatasetService over an injected store and an embedder."""

    def __init__(
        self,
        store: DatasetStore,
        embedder: EmbeddingClient,
        limits: LimitGate | None = None,
    ) -> None:
        self._store = store
        self._author = DatasetAuthor(store, embedder, limits)
        self._search = DatasetSearch(store, embedder)

    async def create_dataset(self, definition: DatasetDefinition) -> Dataset:
        """Embed name + description + usage_notes and insert the descriptor.

        The plan cap is checked here so the agent's implicit creation on a
        first `data_put` goes through the same gate as an explicit one.
        """
        return await self._author.create(definition)

    async def get_dataset(self, owner_user_id: str, name: str) -> Dataset:
        """Return the descriptor of this owner by name."""
        dataset = await self._store.get(owner_user_id, name)
        if dataset is None:
            raise DatasetNotFoundError(name)
        return dataset

    async def add_record(
        self,
        owner_user_id: str,
        dataset_name: str,
        payload: dict[str, Any],
    ) -> DatasetRecord:
        """Append a record; the owner check happens in SQL (WHERE owner_user_id)."""
        dataset = await self._store.get(owner_user_id, dataset_name)
        if dataset is None:
            raise DatasetNotFoundError(dataset_name)
        return await self._store.add_record(dataset.id, owner_user_id, payload)

    async def query_records(self, request: DatasetRecordQuery) -> list[DatasetRecord]:
        """Scan the SQL-side date range, then apply the equals filter in Python.

        Tracker datasets are small, so the payload equality filter runs in
        memory over the bounded scan (MAX_SCAN_ROWS) instead of JSON indexes.
        """
        dataset = await self._store.get(request.owner_user_id, request.dataset_name)
        if dataset is None:
            raise DatasetNotFoundError(request.dataset_name)
        candidates = await self._store.query_candidates(
            DatasetRecordScan(dataset.id, request.date_from, request.date_to, MAX_SCAN_ROWS)
        )
        if request.equals:
            candidates = [
                record for record in candidates if _matches_equals(record.payload, request.equals)
            ]
        return candidates[: request.limit]

    async def delete_dataset(self, owner_user_id: str, name: str) -> int:
        """Delete the descriptor with its records; return the record count."""
        records_count = await self._store.delete(owner_user_id, name)
        if records_count is None:
            raise DatasetNotFoundError(name)
        return records_count

    async def search(self, owner_user_id: str, query: str, k: int) -> list[DatasetHit]:
        """Embed the query and rank this owner's candidates by cosine.

        Candidates come from the store: vector-capable stores run the search
        on their side, the rest hand over all of the owner's descriptors for
        brute-force cosine.
        """
        return await self._search.search(owner_user_id, query, k)


def _matches_equals(payload: dict[str, Any], equals: dict[str, Any]) -> bool:
    """Type-sensitive equality: 5 never equals "5", True never equals 1."""
    for key, expected in equals.items():
        if key not in payload:
            return False
        value = payload[key]
        if type(value) is not type(expected) or value != expected:
            return False
    return True
