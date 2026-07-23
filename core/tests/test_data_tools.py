"""Tests for the dataset runtime tools and the merged skills_search."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.datasets.api import (
    Dataset,
    DatasetField,
    DatasetHit,
    DatasetNotFoundError,
    DatasetRecord,
    DatasetSchema,
    DatasetService,
    FieldType,
)
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.datasets.tools import DataForgetTool, DataPutTool, DataQueryTool
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.instructions.api import (
    Instruction,
    InstructionType,
    SearchHit,
)
from octoforge_core.instructions.tools import (
    NO_HITS_MESSAGE,
    SkillsSearchTool,
)
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
CTX = ToolContext(user_id="user-test", channel="web", dialog_id="dlg-test")
DEFAULT_LIMIT = 2
MAX_LIMIT = 5
THREE_RECORDS = 3
DEFAULT_K = 5
DATASET_VERSION = 1

V_RIGHT = (1.0, 0.0)
CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

FOOD_DATASET = "food_log"
FOOD_DESCRIPTION = "what the user eats"
FOOD_SCHEMA_RAW: dict[str, Any] = {
    "fields": [
        {"name": "item", "type": "string", "required": True},
        {"name": "kcal", "type": "integer"},
    ]
}
APPLE = {"item": "apple", "kcal": 95}
BANANA = {"item": "banana", "kcal": 105}


class StubEmbedder:
    """Deterministic EmbeddingClient: exact mapping with a default fallback."""

    def __init__(self, default: tuple[float, ...] = V_RIGHT) -> None:
        self.vectors: dict[str, tuple[float, ...]] = {}
        self.default = default

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.vectors.get(text, self.default) for text in texts)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def service(session_factory: async_sessionmaker[AsyncSession]) -> DatasetService:
    return LocalDatasetService(SqlAlchemyDatasetStore(session_factory), StubEmbedder())


async def create_food_dataset(service: DatasetService) -> Dataset:
    return await service.create_dataset(
        CTX.user_id,
        FOOD_DATASET,
        FOOD_DESCRIPTION,
        DatasetSchema(
            (
                DatasetField(name="item", type=FieldType.STRING, required=True),
                DatasetField(name="kcal", type=FieldType.INTEGER, required=False),
            )
        ),
    )


# --- data_put ---------------------------------------------------------------


def test_put_skill_spec(service: DatasetService) -> None:
    tool = DataPutTool(service=service)

    assert tool.spec.name == "data_put"
    assert tool.spec.parameters_schema["required"] == ["dataset", "record"]


async def test_put_creates_dataset_and_record(service: DatasetService) -> None:
    tool = DataPutTool(service=service)

    output = await tool.execute(
        {
            "dataset": FOOD_DATASET,
            "record": APPLE,
            "description": FOOD_DESCRIPTION,
            "schema": FOOD_SCHEMA_RAW,
            "usage_notes": "one record per meal",
            "retention": "keep forever",
        },
        CTX,
    )

    assert output.startswith(f"dataset '{FOOD_DATASET}' created; record ")
    assert " added at " in output
    dataset = await service.get_dataset(CTX.user_id, FOOD_DATASET)
    assert dataset.usage_notes == "one record per meal"
    assert dataset.retention == "keep forever"
    records = await service.query_records(CTX.user_id, FOOD_DATASET, None, None, None, 10)
    assert [record.payload for record in records] == [APPLE]


async def test_put_into_existing_dataset(service: DatasetService) -> None:
    await create_food_dataset(service)
    tool = DataPutTool(service=service)

    output = await tool.execute({"dataset": FOOD_DATASET, "record": APPLE}, CTX)

    assert output.startswith("record ")
    assert f" added to dataset '{FOOD_DATASET}' at " in output


@pytest.mark.parametrize(
    "arguments",
    [
        {"dataset": FOOD_DATASET, "record": APPLE},
        {
            "dataset": FOOD_DATASET,
            "record": APPLE,
            "schema": FOOD_SCHEMA_RAW,
        },
        {
            "dataset": FOOD_DATASET,
            "record": APPLE,
            "description": FOOD_DESCRIPTION,
        },
    ],
)
async def test_put_creation_requires_schema_and_description(
    service: DatasetService,
    arguments: dict[str, Any],
) -> None:
    tool = DataPutTool(service=service)

    with pytest.raises(ToolArgumentsError, match="will be created"):
        await tool.execute(arguments, CTX)

    with pytest.raises(DatasetNotFoundError):
        await service.get_dataset(CTX.user_id, FOOD_DATASET)


async def test_put_invalid_schema_rejected(service: DatasetService) -> None:
    tool = DataPutTool(service=service)
    arguments = {
        "dataset": FOOD_DATASET,
        "record": APPLE,
        "description": FOOD_DESCRIPTION,
        "schema": {"fields": [{"name": "item", "type": "unknown"}]},
    }

    with pytest.raises(ToolArgumentsError, match="invalid schema"):
        await tool.execute(arguments, CTX)


async def test_put_record_violation_reported(service: DatasetService) -> None:
    await create_food_dataset(service)
    tool = DataPutTool(service=service)

    with pytest.raises(ToolArgumentsError, match="'item' is required"):
        await tool.execute({"dataset": FOOD_DATASET, "record": {"kcal": 95}}, CTX)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"dataset": "", "record": APPLE},
        {"dataset": 42, "record": APPLE},
        {"dataset": FOOD_DATASET},
        {"dataset": FOOD_DATASET, "record": "not-an-object"},
        {
            "dataset": FOOD_DATASET,
            "record": APPLE,
            "description": FOOD_DESCRIPTION,
            "schema": FOOD_SCHEMA_RAW,
            "retention": 42,
        },
    ],
)
async def test_put_invalid_arguments_rejected(
    service: DatasetService,
    arguments: dict[str, Any],
) -> None:
    tool = DataPutTool(service=service)

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)


# --- data_query -------------------------------------------------------------


def make_query_tool(service: DatasetService) -> DataQueryTool:
    return DataQueryTool(service=service, default_limit=DEFAULT_LIMIT, max_limit=MAX_LIMIT)


def test_query_skill_spec(service: DatasetService) -> None:
    tool = make_query_tool(service)

    assert tool.spec.name == "data_query"
    assert tool.spec.parameters_schema["required"] == ["dataset"]


async def seed_three_records(service: DatasetService) -> None:
    await create_food_dataset(service)
    for seq in range(THREE_RECORDS):
        await service.add_record(CTX.user_id, FOOD_DATASET, {"item": f"item-{seq}", "seq": seq})


async def test_query_formats_records_as_json_lines(service: DatasetService) -> None:
    await seed_three_records(service)
    tool = make_query_tool(service)

    output = await tool.execute({"dataset": FOOD_DATASET, "limit": MAX_LIMIT}, CTX)

    header, *lines = output.splitlines()
    assert header == f"{THREE_RECORDS} record(s) in dataset '{FOOD_DATASET}' (newest first):"
    records = [json.loads(line) for line in lines]
    assert [record["payload"]["seq"] for record in records] == [2, 1, 0]
    for record in records:
        assert record["id"]
        assert datetime.fromisoformat(record["created_at"]).tzinfo == UTC


async def test_query_equals_filter(service: DatasetService) -> None:
    await create_food_dataset(service)
    await service.add_record(CTX.user_id, FOOD_DATASET, APPLE)
    await service.add_record(CTX.user_id, FOOD_DATASET, BANANA)
    tool = make_query_tool(service)

    output = await tool.execute({"dataset": FOOD_DATASET, "equals": {"item": "apple"}}, CTX)

    header, *lines = output.splitlines()
    assert header.startswith("1 record(s)")
    assert json.loads(lines[0])["payload"] == APPLE


async def test_query_default_limit_applies(service: DatasetService) -> None:
    await seed_three_records(service)
    tool = make_query_tool(service)

    output = await tool.execute({"dataset": FOOD_DATASET}, CTX)

    header, *lines = output.splitlines()
    assert header.startswith(f"{DEFAULT_LIMIT} record(s)")
    assert len(lines) == DEFAULT_LIMIT


async def test_query_date_only_boundaries(service: DatasetService) -> None:
    await seed_three_records(service)
    tool = make_query_tool(service)
    today = utc_now().date().isoformat()
    tomorrow = (utc_now().date() + timedelta(days=1)).isoformat()

    inside = await tool.execute(
        {"dataset": FOOD_DATASET, "date_from": today, "date_to": today}, CTX
    )
    outside = await tool.execute({"dataset": FOOD_DATASET, "date_from": tomorrow}, CTX)

    assert inside.startswith(f"{DEFAULT_LIMIT} record(s)")
    assert outside == f"no records in dataset '{FOOD_DATASET}'"


async def test_query_unknown_dataset_returns_text(service: DatasetService) -> None:
    tool = make_query_tool(service)

    output = await tool.execute({"dataset": "missing"}, CTX)

    assert output == "dataset 'missing' not found"


async def test_query_empty_result_text(service: DatasetService) -> None:
    await create_food_dataset(service)
    tool = make_query_tool(service)

    assert await tool.execute({"dataset": FOOD_DATASET}, CTX) == (
        f"no records in dataset '{FOOD_DATASET}'"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"dataset": "  "},
        {"dataset": FOOD_DATASET, "equals": "not-an-object"},
        {"dataset": FOOD_DATASET, "equals": {1: "x"}},
        {"dataset": FOOD_DATASET, "date_from": "not-a-date"},
        {"dataset": FOOD_DATASET, "date_from": 42},
        {"dataset": FOOD_DATASET, "date_to": True},
        {"dataset": FOOD_DATASET, "limit": 0},
        {"dataset": FOOD_DATASET, "limit": MAX_LIMIT + 1},
        {"dataset": FOOD_DATASET, "limit": "3"},
        {"dataset": FOOD_DATASET, "limit": True},
    ],
)
async def test_query_invalid_arguments_rejected(
    service: DatasetService,
    arguments: dict[str, Any],
) -> None:
    await create_food_dataset(service)
    tool = make_query_tool(service)

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)


# --- data_forget ------------------------------------------------------------


def test_forget_skill_spec(service: DatasetService) -> None:
    tool = DataForgetTool(service=service)

    assert tool.spec.name == "data_forget"
    assert tool.spec.parameters_schema["required"] == ["dataset"]


async def test_forget_deletes_and_reports_count(service: DatasetService) -> None:
    await create_food_dataset(service)
    await service.add_record(CTX.user_id, FOOD_DATASET, APPLE)
    await service.add_record(CTX.user_id, FOOD_DATASET, BANANA)
    tool = DataForgetTool(service=service)

    output = await tool.execute({"dataset": FOOD_DATASET}, CTX)

    assert output == f"dataset '{FOOD_DATASET}' deleted with 2 record(s)"
    assert await tool.execute({"dataset": FOOD_DATASET}, CTX) == (
        f"dataset '{FOOD_DATASET}' not found"
    )


async def test_forget_unknown_dataset_returns_text(service: DatasetService) -> None:
    tool = DataForgetTool(service=service)

    assert await tool.execute({"dataset": "missing"}, CTX) == "dataset 'missing' not found"


@pytest.mark.parametrize("arguments", [{}, {"dataset": ""}, {"dataset": 42}])
async def test_forget_invalid_arguments_rejected(
    service: DatasetService,
    arguments: dict[str, Any],
) -> None:
    tool = DataForgetTool(service=service)

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)


# --- skills_search with datasets --------------------------------------

HIT = SearchHit(
    instruction=Instruction(
        id="id-1",
        type=InstructionType.KNOWLEDGE,
        title="nutrition facts",
        content="kcal tables",
        tags=("food",),
        version=1,
        usage_count=0,
        success_count=0,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    ),
    # a cross-encoder logit (rerank stage) — the scales of instruction and
    # dataset scores are incomparable, so the blocks must not merge by score
    score=-3.2,
)


def make_food_dataset_dto() -> Dataset:
    return Dataset(
        id="id-food",
        owner_user_id=CTX.user_id,
        name=FOOD_DATASET,
        description=FOOD_DESCRIPTION,
        schema=DatasetSchema(
            (
                DatasetField(name="item", type=FieldType.STRING, required=True),
                DatasetField(name="kcal", type=FieldType.INTEGER, required=False),
            )
        ),
        usage_notes="",
        retention="",
        version=DATASET_VERSION,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


DATASET_HIT = DatasetHit(dataset=make_food_dataset_dto(), score=0.9)


class FakeInstructionService:
    """InstructionService stub with scripted hits."""

    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits or []

    async def search(self, query: str, k: int) -> list[SearchHit]:
        return self.hits

    async def save(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        raise NotImplementedError

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
        raise NotImplementedError


class FakeDatasetService:
    """DatasetService stub with scripted search hits; the rest is unused."""

    def __init__(self, hits: list[DatasetHit] | None = None) -> None:
        self.hits = hits or []
        self.search_calls: list[tuple[str, str, int]] = []

    async def search(self, owner_user_id: str, query: str, k: int) -> list[DatasetHit]:
        self.search_calls.append((owner_user_id, query, k))
        return self.hits

    async def create_dataset(  # noqa: PLR0913 — protocol-shaped stub
        self,
        owner_user_id: str,
        name: str,
        description: str,
        schema: DatasetSchema,
        usage_notes: str = "",
        retention: str = "",
    ) -> Dataset:
        raise NotImplementedError

    async def get_dataset(self, owner_user_id: str, name: str) -> Dataset:
        raise NotImplementedError

    async def add_record(
        self,
        owner_user_id: str,
        dataset_name: str,
        payload: dict[str, Any],
    ) -> DatasetRecord:
        raise NotImplementedError

    async def query_records(  # noqa: PLR0913 — protocol-shaped stub
        self,
        owner_user_id: str,
        dataset_name: str,
        equals: dict[str, Any] | None,
        date_from: datetime | None,
        date_to: datetime | None,
        limit: int,
    ) -> list[DatasetRecord]:
        raise NotImplementedError

    async def delete_dataset(self, owner_user_id: str, name: str) -> int:
        raise NotImplementedError


async def test_search_instructions_block_precedes_datasets() -> None:
    datasets = FakeDatasetService(hits=[DATASET_HIT])
    tool = SkillsSearchTool(
        service=FakeInstructionService(hits=[HIT]),
        default_k=DEFAULT_K,
        datasets=datasets,
    )

    output = await tool.execute({"query": "food"}, CTX)

    # instructions first (even with a negative cross-encoder logit against a
    # higher dataset cosine), datasets after; no scores in the output
    lines = output.splitlines()
    assert lines[0] == "1. [knowledge] nutrition facts"
    assert lines[1] == "   tags: food"
    assert lines[2] == "kcal tables"
    assert lines[3] == f"2. [dataset] {FOOD_DATASET}"
    assert lines[4] == "   fields: item, kcal"
    assert lines[5] == f"   {FOOD_DESCRIPTION}"
    assert "score" not in output
    assert datasets.search_calls == [(CTX.user_id, "food", DEFAULT_K)]


async def test_search_dataset_hits_only() -> None:
    datasets = FakeDatasetService(hits=[DATASET_HIT])
    tool = SkillsSearchTool(
        service=FakeInstructionService(),
        default_k=DEFAULT_K,
        datasets=datasets,
    )

    output = await tool.execute({"query": "food"}, CTX)

    assert "[dataset]" in output


async def test_search_without_datasets_service_unchanged() -> None:
    tool = SkillsSearchTool(service=FakeInstructionService(), default_k=DEFAULT_K)

    assert await tool.execute({"query": "nothing"}, CTX) == NO_HITS_MESSAGE
