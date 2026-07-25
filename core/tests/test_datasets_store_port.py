"""Substitution tests for the DatasetStore port.

An in-memory store is injected into the unmodified LocalDatasetService,
proving the storage layer is replaceable without touching the core (the
modularity roadmap, P1): brute-force ranking flows through
`list_with_embeddings`, vector-capable stores get `search_by_vector` instead.
"""

from datetime import datetime
from typing import Any

import pytest

from octoforge_core.datasets.api import (
    Dataset,
    DatasetExistsError,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetSchema,
    EmbeddedDataset,
)
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.time import utc_now

OWNER_A = "user-a"
OWNER_B = "user-b"
FOOD_DATASET = "food_log"
SPORT_DATASET = "sport_log"
DESCRIPTION = "tracker"
USAGE_NOTES = ""
RETENTION = ""
QUERY = "find something"
FIRST_VERSION = 1
TWO_RECORDS = 2

V_RIGHT = (1.0, 0.0)
V_QUERY = (0.9, 0.1)

APPLE = {"item": "apple", "kcal": 95}
BANANA = {"item": "banana", "kcal": 105}

EMPTY_SCHEMA = DatasetSchema(())


class StubEmbedder:
    """Deterministic EmbeddingClient: every text maps to the default vector."""

    def __init__(self, default: tuple[float, ...] = V_RIGHT) -> None:
        self.default = default

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.default for _ in texts)


class InMemoryDatasetStore:
    """DatasetStore implementation keeping descriptors and records in dicts."""

    def __init__(self) -> None:
        self.datasets: dict[tuple[str, str], Dataset] = {}
        self.embeddings: dict[str, tuple[float, ...]] = {}
        self.records: list[DatasetRecord] = []
        self.list_calls = 0

    async def create(  # noqa: PLR0913, PLR0917 — mirrors the DatasetStore port signature
        self,
        owner_user_id: str,
        name: str,
        description: str,
        schema: DatasetSchema,
        usage_notes: str,
        retention: str,
        embedding: tuple[float, ...],
    ) -> Dataset:
        key = (owner_user_id, name)
        if key in self.datasets:
            raise DatasetExistsError(f"dataset '{name}' already exists")
        now = utc_now()
        dataset = Dataset(
            id=f"mem-{len(self.datasets)}",
            owner_user_id=owner_user_id,
            name=name,
            description=description,
            schema=schema,
            usage_notes=usage_notes,
            retention=retention,
            version=FIRST_VERSION,
            created_at=now,
            updated_at=now,
        )
        self.datasets[key] = dataset
        self.embeddings[dataset.id] = embedding
        return dataset

    async def get(self, owner_user_id: str, name: str) -> Dataset | None:
        return self.datasets.get((owner_user_id, name))

    async def add_record(
        self,
        dataset_id: str,
        owner_user_id: str,
        payload: dict[str, Any],
    ) -> DatasetRecord:
        record = DatasetRecord(
            id=f"rec-{len(self.records)}",
            dataset_id=dataset_id,
            owner_user_id=owner_user_id,
            payload=payload,
            created_at=utc_now(),
        )
        self.records.append(record)
        return record

    async def delete(self, owner_user_id: str, name: str) -> int | None:
        dataset = self.datasets.pop((owner_user_id, name), None)
        if dataset is None:
            return None
        kept = [record for record in self.records if record.dataset_id != dataset.id]
        deleted = len(self.records) - len(kept)
        self.records = kept
        return deleted

    async def list_with_embeddings(self, owner_user_id: str) -> list[EmbeddedDataset]:
        self.list_calls += 1
        return self._owned(owner_user_id)

    async def query_candidates(
        self,
        dataset_id: str,
        date_from: datetime | None,
        date_to: datetime | None,
        scan_limit: int,
    ) -> list[DatasetRecord]:
        candidates = [
            record
            for record in self.records
            if record.dataset_id == dataset_id
            and (date_from is None or record.created_at >= date_from)
            and (date_to is None or record.created_at <= date_to)
        ]
        candidates.sort(key=lambda record: (record.created_at, record.id), reverse=True)
        return candidates[:scan_limit]

    def _owned(self, owner_user_id: str) -> list[EmbeddedDataset]:
        return [
            EmbeddedDataset(dataset=dataset, embedding=self.embeddings[dataset.id])
            for dataset in self.datasets.values()
            if dataset.owner_user_id == owner_user_id
        ]


class VectorSearchStore(InMemoryDatasetStore):
    """Adds the DatasetVectorSearch capability: the store runs the search."""

    def __init__(self) -> None:
        super().__init__()
        self.vector_calls: list[tuple[str, tuple[float, ...], int]] = []

    async def search_by_vector(
        self,
        owner_user_id: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[EmbeddedDataset]:
        self.vector_calls.append((owner_user_id, query_embedding, limit))
        return self._owned(owner_user_id)[:limit]


def make_service(store: InMemoryDatasetStore) -> LocalDatasetService:
    return LocalDatasetService(store, StubEmbedder())


async def test_service_runs_over_an_in_memory_store() -> None:
    store = InMemoryDatasetStore()
    service = make_service(store)

    await service.create_dataset(OWNER_A, FOOD_DATASET, DESCRIPTION, EMPTY_SCHEMA)
    await service.add_record(OWNER_A, FOOD_DATASET, APPLE)
    await service.add_record(OWNER_A, FOOD_DATASET, BANANA)

    records = await service.query_records(OWNER_A, FOOD_DATASET, None, None, None, TWO_RECORDS)
    assert [record.payload for record in records] == [BANANA, APPLE]

    filtered = await service.query_records(
        OWNER_A, FOOD_DATASET, {"item": "apple"}, None, None, TWO_RECORDS
    )
    assert [record.payload for record in filtered] == [APPLE]

    deleted = await service.delete_dataset(OWNER_A, FOOD_DATASET)
    assert deleted == TWO_RECORDS
    with pytest.raises(DatasetNotFoundError):
        await service.get_dataset(OWNER_A, FOOD_DATASET)


async def test_vector_capable_store_receives_the_search_with_the_owner() -> None:
    store = VectorSearchStore()
    service = make_service(store)
    await service.create_dataset(OWNER_A, FOOD_DATASET, DESCRIPTION, EMPTY_SCHEMA)
    await service.create_dataset(OWNER_A, SPORT_DATASET, DESCRIPTION, EMPTY_SCHEMA)
    await service.create_dataset(OWNER_B, FOOD_DATASET, DESCRIPTION, EMPTY_SCHEMA)

    hits = await service.search(OWNER_A, QUERY, k=1)

    assert store.vector_calls == [(OWNER_A, V_RIGHT, 1)]
    assert store.list_calls == 0  # the brute-force path was not used
    assert len(hits) == 1
    assert hits[0].dataset.owner_user_id == OWNER_A
