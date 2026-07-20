"""Local in-process implementation of the DatasetService facade.

The storage layer is the `DatasetStore` port injected through the
constructor: the shipped `SqlAlchemyDatasetStore` ranks brute-force in the
process, an installer can substitute a pgvector/vector-DB store
(implementing `DatasetVectorSearch`) without touching this service.
"""

from datetime import datetime
from typing import Any

from octoforge_core.datasets.api import (
    Dataset,
    DatasetHit,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetSchema,
    DatasetStore,
    DatasetVectorSearch,
    EmbeddedDataset,
)
from octoforge_core.datasets.ranking import rank
from octoforge_core.llm.embeddings import EmbeddingClient

EMBEDDED_TEXT_SEPARATOR = "\n"
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
    ) -> None:
        self._store = store
        self._embedder = embedder

    async def create_dataset(  # noqa: PLR0913 — facade signature from the module boundary
        self,
        owner_user_id: str,
        name: str,
        description: str,
        schema: DatasetSchema,
        usage_notes: str = "",
        retention: str = "",
    ) -> Dataset:
        """Embed name + description + usage_notes and insert the descriptor."""
        (embedding,) = await self._embedder.embed((_embedded_text(name, description, usage_notes),))
        return await self._store.create(
            owner_user_id, name, description, schema, usage_notes, retention, embedding
        )

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

    async def query_records(  # noqa: PLR0913 — facade signature from the module boundary
        self,
        owner_user_id: str,
        dataset_name: str,
        equals: dict[str, Any] | None,
        date_from: datetime | None,
        date_to: datetime | None,
        limit: int,
    ) -> list[DatasetRecord]:
        """Scan the SQL-side date range, then apply the equals filter in Python.

        Tracker datasets are small, so the payload equality filter runs in
        memory over the bounded scan (MAX_SCAN_ROWS) instead of JSON indexes.
        """
        dataset = await self._store.get(owner_user_id, dataset_name)
        if dataset is None:
            raise DatasetNotFoundError(dataset_name)
        candidates = await self._store.query_candidates(
            dataset.id, date_from, date_to, MAX_SCAN_ROWS
        )
        if equals:
            candidates = [
                record for record in candidates if _matches_equals(record.payload, equals)
            ]
        return candidates[:limit]

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
        if not query.strip():
            return []
        (query_embedding,) = await self._embedder.embed((query,))
        candidates = await self._candidates(owner_user_id, query_embedding, k)
        return rank(candidates, query, query_embedding, k)

    async def _candidates(
        self,
        owner_user_id: str,
        query_embedding: tuple[float, ...],
        k: int,
    ) -> list[EmbeddedDataset]:
        """Fetch the ranking input: vector search on the store side when supported."""
        if isinstance(self._store, DatasetVectorSearch):
            return await self._store.search_by_vector(owner_user_id, query_embedding, k)
        return await self._store.list_with_embeddings(owner_user_id)


def _embedded_text(name: str, description: str, usage_notes: str) -> str:
    return EMBEDDED_TEXT_SEPARATOR.join((name, description, usage_notes))


def _matches_equals(payload: dict[str, Any], equals: dict[str, Any]) -> bool:
    """Type-sensitive equality: 5 never equals "5", True never equals 1."""
    for key, expected in equals.items():
        if key not in payload:
            return False
        value = payload[key]
        if type(value) is not type(expected) or value != expected:
            return False
    return True
