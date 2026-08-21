"""Contract-style tests for DatasetService implementations.

The suite talks to the facade only (create/get/add_record/query_records/
delete_dataset/search) and builds the service under test through the
`service_factory` fixture, so the same suite can later validate an HTTP
implementation of the protocol by swapping that one fixture.
"""

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.datasets.api import (
    Dataset,
    DatasetDefinition,
    DatasetExistsError,
    DatasetNotFoundError,
    DatasetQuotaError,
    DatasetRecordQuery,
    DatasetSchema,
    DatasetService,
    FieldType,
)
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.datasets.validation import parse_schema
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.tariffs.api import LimitVerdict, UsageEvent
from octoforge_core.time import utc_now

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
EMBEDDED_TEXT_SEPARATOR = "\n"
VERSION_CREATED = 1
TWO_RECORDS = 2
THREE_RECORDS = 3
TWO_HITS = 2

V_RIGHT = (1.0, 0.0)
V_UP = (0.0, 1.0)
V_DIAGONAL = (0.6, 0.8)

OWNER_A = "user-a"
OWNER_B = "user-b"
FOOD_DATASET = "food_log"
SPORT_DATASET = "sport_log"
FOOD_DESCRIPTION = "what the user eats"
SPORT_DESCRIPTION = "workouts"
QUERY = "find something"
EXACT_QUERY = "FOOD_LOG"  # matches FOOD_DATASET case-insensitively

FOOD_SCHEMA_RAW = {
    "fields": [
        {"name": "item", "type": "string", "required": True},
        {"name": "kcal", "type": "integer"},
    ]
}
FOOD_SCHEMA = parse_schema(FOOD_SCHEMA_RAW)
FOOD_DEFINITION = DatasetDefinition(OWNER_A, FOOD_DATASET, FOOD_DESCRIPTION, FOOD_SCHEMA)
SPORT_DEFINITION = DatasetDefinition(OWNER_A, SPORT_DATASET, SPORT_DESCRIPTION, DatasetSchema(()))
BASE_RECORD_QUERY = DatasetRecordQuery(OWNER_A, FOOD_DATASET, None, None, None, 10)


class StubEmbedder:
    """Deterministic EmbeddingClient: exact text-to-vector mapping."""

    def __init__(self) -> None:
        self.vectors: dict[str, tuple[float, ...]] = {}
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple(self.vectors[text] for text in texts)


ServiceFactory = Callable[[async_sessionmaker[AsyncSession], EmbeddingClient], DatasetService]


def build_local_service(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
) -> DatasetService:
    """Assemble the default local implementation over the SQL store."""
    return LocalDatasetService(SqlAlchemyDatasetStore(session_factory), embedder)


@pytest.fixture
def service_factory() -> ServiceFactory:
    """The implementation under test; swap to run the suite over another one."""
    return build_local_service


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def embedder() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def service(
    service_factory: ServiceFactory,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: StubEmbedder,
) -> DatasetService:
    return service_factory(session_factory, embedder)


def register_vector(
    embedder: StubEmbedder,
    definition: DatasetDefinition,
    vector: tuple[float, ...],
) -> None:
    """Map the exact text the service embeds for a descriptor to a vector."""
    key = EMBEDDED_TEXT_SEPARATOR.join(
        (definition.name, definition.description, definition.usage_notes)
    )
    embedder.vectors[key] = vector


async def create_food_dataset(service: DatasetService, owner: str = OWNER_A) -> Dataset:
    return await service.create_dataset(replace(FOOD_DEFINITION, owner_user_id=owner))


async def test_create_and_get_dataset(service: DatasetService, embedder: StubEmbedder) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)

    created = await create_food_dataset(service)

    assert created.id
    assert created.owner_user_id == OWNER_A
    assert created.name == FOOD_DATASET
    assert created.description == FOOD_DESCRIPTION
    assert created.schema == FOOD_SCHEMA
    assert created.usage_notes == ""
    assert created.retention == ""
    assert created.version == VERSION_CREATED
    assert created.created_at.tzinfo == UTC
    assert created.updated_at.tzinfo == UTC
    stored = await service.get_dataset(OWNER_A, FOOD_DATASET)
    assert stored == created
    assert [field.type for field in stored.schema.fields] == [
        FieldType.STRING,
        FieldType.INTEGER,
    ]


async def test_create_duplicate_name_same_owner_raises(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    await create_food_dataset(service)

    with pytest.raises(DatasetExistsError):
        await create_food_dataset(service)


async def test_same_name_different_owners_coexist(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)

    first = await service.create_dataset(FOOD_DEFINITION)
    second = await service.create_dataset(replace(FOOD_DEFINITION, owner_user_id=OWNER_B))

    assert first.id != second.id
    assert (await service.get_dataset(OWNER_B, FOOD_DATASET)).id == second.id


async def test_get_missing_dataset_raises(service: DatasetService) -> None:
    with pytest.raises(DatasetNotFoundError):
        await service.get_dataset(OWNER_A, "missing")


async def test_add_record_round_trip(service: DatasetService, embedder: StubEmbedder) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    dataset = await create_food_dataset(service)
    payload = {"item": "apple", "kcal": 95}

    record = await service.add_record(OWNER_A, FOOD_DATASET, payload)

    assert record.id
    assert record.dataset_id == dataset.id
    assert record.owner_user_id == OWNER_A
    assert record.payload == payload
    assert record.created_at.tzinfo == UTC


async def test_add_record_unknown_dataset_raises(service: DatasetService) -> None:
    with pytest.raises(DatasetNotFoundError):
        await service.add_record(OWNER_A, "missing", {"item": "apple"})


async def test_owner_isolation(service: DatasetService, embedder: StubEmbedder) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    await create_food_dataset(service)
    embedder.vectors[QUERY] = V_RIGHT

    with pytest.raises(DatasetNotFoundError):
        await service.get_dataset(OWNER_B, FOOD_DATASET)
    with pytest.raises(DatasetNotFoundError):
        await service.add_record(OWNER_B, FOOD_DATASET, {"item": "apple"})
    with pytest.raises(DatasetNotFoundError):
        await service.query_records(replace(BASE_RECORD_QUERY, owner_user_id=OWNER_B, limit=1))
    with pytest.raises(DatasetNotFoundError):
        await service.delete_dataset(OWNER_B, FOOD_DATASET)
    assert await service.search(OWNER_B, QUERY, k=TWO_HITS) == []
    assert len(await service.search(OWNER_A, QUERY, k=TWO_HITS)) == 1


async def test_query_records_newest_first(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    await create_food_dataset(service)
    for seq in range(THREE_RECORDS):
        await service.add_record(OWNER_A, FOOD_DATASET, {"item": f"item-{seq}", "seq": seq})

    records = await service.query_records(BASE_RECORD_QUERY)

    assert [record.payload["seq"] for record in records] == [2, 1, 0]


async def test_query_records_equals_is_type_sensitive(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    await create_food_dataset(service)
    await service.add_record(OWNER_A, FOOD_DATASET, {"item": "apple", "kcal": 95})
    await service.add_record(OWNER_A, FOOD_DATASET, {"item": "apple", "kcal": "95"})
    await service.add_record(OWNER_A, FOOD_DATASET, {"item": "banana", "kcal": 105})

    by_item = await service.query_records(replace(BASE_RECORD_QUERY, equals={"item": "apple"}))
    by_int = await service.query_records(replace(BASE_RECORD_QUERY, equals={"kcal": 95}))
    by_str = await service.query_records(replace(BASE_RECORD_QUERY, equals={"kcal": "95"}))
    by_missing = await service.query_records(replace(BASE_RECORD_QUERY, equals={"fat": 1}))

    assert len(by_item) == TWO_RECORDS
    assert [record.payload["item"] for record in by_int] == ["apple"]
    assert [record.payload["item"] for record in by_str] == ["apple"]
    assert by_missing == []


async def test_query_records_date_range(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    await create_food_dataset(service)
    started = utc_now()
    await service.add_record(OWNER_A, FOOD_DATASET, {"item": "apple"})
    finished = utc_now()
    day = timedelta(days=1)

    wide = await service.query_records(
        replace(BASE_RECORD_QUERY, date_from=started - day, date_to=finished + day)
    )
    exact = await service.query_records(
        replace(BASE_RECORD_QUERY, date_from=started, date_to=finished)
    )
    future = await service.query_records(
        replace(BASE_RECORD_QUERY, date_from=finished + day, date_to=finished + day)
    )
    past = await service.query_records(
        replace(BASE_RECORD_QUERY, date_from=started - day, date_to=started - day)
    )
    from_only = await service.query_records(replace(BASE_RECORD_QUERY, date_from=started - day))
    to_only = await service.query_records(replace(BASE_RECORD_QUERY, date_to=finished + day))

    assert len(wide) == 1
    assert len(exact) == 1
    assert future == []
    assert past == []
    assert len(from_only) == 1
    assert len(to_only) == 1


async def test_query_records_limit(service: DatasetService, embedder: StubEmbedder) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    await create_food_dataset(service)
    for seq in range(THREE_RECORDS):
        await service.add_record(OWNER_A, FOOD_DATASET, {"item": f"item-{seq}", "seq": seq})

    records = await service.query_records(replace(BASE_RECORD_QUERY, limit=TWO_RECORDS))

    assert [record.payload["seq"] for record in records] == [2, 1]


async def test_query_records_unknown_dataset_raises(service: DatasetService) -> None:
    with pytest.raises(DatasetNotFoundError):
        await service.query_records(replace(BASE_RECORD_QUERY, dataset_name="missing", limit=1))


async def test_delete_dataset_cascades_and_counts(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    register_vector(embedder, SPORT_DEFINITION, V_UP)
    await create_food_dataset(service)
    await service.create_dataset(SPORT_DEFINITION)
    for _ in range(TWO_RECORDS):
        await service.add_record(OWNER_A, FOOD_DATASET, {"item": "apple"})
    await service.add_record(OWNER_A, SPORT_DATASET, {"kind": "run"})

    deleted = await service.delete_dataset(OWNER_A, FOOD_DATASET)

    assert deleted == TWO_RECORDS
    with pytest.raises(DatasetNotFoundError):
        await service.get_dataset(OWNER_A, FOOD_DATASET)
    remaining = await service.query_records(replace(BASE_RECORD_QUERY, dataset_name=SPORT_DATASET))
    assert len(remaining) == 1


async def test_delete_missing_dataset_raises(service: DatasetService) -> None:
    with pytest.raises(DatasetNotFoundError):
        await service.delete_dataset(OWNER_A, "missing")


async def test_search_closer_vector_wins(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    register_vector(embedder, SPORT_DEFINITION, V_UP)
    await create_food_dataset(service)
    await service.create_dataset(SPORT_DEFINITION)
    embedder.vectors[QUERY] = V_DIAGONAL

    hits = await service.search(OWNER_A, QUERY, k=TWO_HITS)

    assert [hit.dataset.name for hit in hits] == [SPORT_DATASET, FOOD_DATASET]
    assert hits[0].score == pytest.approx(0.8)
    assert hits[1].score == pytest.approx(0.6)


async def test_search_exact_name_boost_beats_closer_vector(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, FOOD_DEFINITION, V_UP)
    register_vector(embedder, SPORT_DEFINITION, V_RIGHT)
    await create_food_dataset(service)
    await service.create_dataset(SPORT_DEFINITION)
    # the query vector is closest to sport_log, but the query equals food_log's name
    embedder.vectors[EXACT_QUERY] = V_RIGHT

    hits = await service.search(OWNER_A, EXACT_QUERY, k=TWO_HITS)

    assert [hit.dataset.name for hit in hits] == [FOOD_DATASET, SPORT_DATASET]
    assert hits[0].score > hits[1].score


async def test_search_respects_k(service: DatasetService, embedder: StubEmbedder) -> None:
    for index in range(THREE_RECORDS):
        name = f"dataset-{index}"
        definition = DatasetDefinition(OWNER_A, name, FOOD_DESCRIPTION, DatasetSchema(()))
        register_vector(embedder, definition, V_RIGHT)
        await service.create_dataset(definition)
    embedder.vectors[QUERY] = V_RIGHT

    hits = await service.search(OWNER_A, QUERY, k=TWO_HITS)

    assert len(hits) == TWO_HITS


async def test_search_blank_query_short_circuits(
    service: DatasetService,
    embedder: StubEmbedder,
) -> None:
    assert await service.search(OWNER_A, "   ", k=1) == []
    assert embedder.calls == []


class DatasetCapGate:
    """LimitGate stub with a configurable dataset cap; everything else open."""

    def __init__(self, max_datasets: int | None) -> None:
        self._max_datasets = max_datasets

    async def enabled_features(self, user_id: str) -> frozenset[str] | None:
        return None

    async def allows(self, user_id: str, feature: str) -> bool:
        return True

    async def check_run_budget(self, user_id: str) -> LimitVerdict:
        return LimitVerdict.ok()

    async def max_cron_jobs(self, user_id: str) -> int | None:
        return None

    async def max_datasets(self, user_id: str) -> int | None:
        return self._max_datasets

    async def max_memory_chars(self, user_id: str) -> int | None:
        return None

    async def record(self, event: UsageEvent) -> None:
        return None


async def test_dataset_creation_refuses_over_the_plans_cap(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: StubEmbedder,
) -> None:
    service = LocalDatasetService(
        SqlAlchemyDatasetStore(session_factory), embedder, limits=DatasetCapGate(max_datasets=1)
    )
    second = DatasetDefinition(OWNER_A, "second", FOOD_DESCRIPTION, DatasetSchema(()))
    register_vector(embedder, FOOD_DEFINITION, V_RIGHT)
    register_vector(embedder, second, V_RIGHT)

    await create_food_dataset(service)
    with pytest.raises(DatasetQuotaError, match="at most 1 datasets"):
        await service.create_dataset(second)

    # another owner is unaffected, and an unlimited plan (no cap) stays open
    open_service = LocalDatasetService(
        SqlAlchemyDatasetStore(session_factory), embedder, limits=DatasetCapGate(max_datasets=None)
    )
    await open_service.create_dataset(second)
