"""Local in-process implementation of the DatasetService facade.

SQL storage + brute-force cosine ranking; chosen in the composition root and
replaceable (e.g. by an HTTP client of a dedicated datasets service) without
changes to call sites.
"""

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.datasets.api import (
    Dataset,
    DatasetHit,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetSchema,
)
from octoforge_core.datasets.ranking import rank
from octoforge_core.datasets.store import MAX_SCAN_ROWS, DatasetStore
from octoforge_core.llm.embeddings import EmbeddingClient

EMBEDDED_TEXT_SEPARATOR = "\n"


class LocalDatasetService:
    """DatasetService over the module-owned SQL tables and an embedder."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingClient,
    ) -> None:
        self._store = DatasetStore(session_factory)
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
        """Embed the query and rank this owner's descriptors by cosine."""
        if not query.strip():
            return []
        (query_embedding,) = await self._embedder.embed((query,))
        candidates = await self._store.list_with_embeddings(owner_user_id)
        return rank(candidates, query, query_embedding, k)


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
