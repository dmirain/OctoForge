"""Tests for the memory tools over the shared instruction store (type=memory)."""

from collections.abc import AsyncIterator

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.instructions.api import (
    InstructionDraft,
    InstructionService,
    InstructionType,
)
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.memory.tools import MemoryDeleteTool, MemoryStoreTool
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
CTX_A = ToolContext(user_id="user-a", channel="web", dialog_id="dlg-a")
CTX_A_OTHER_SURFACE = ToolContext(user_id="user-a", channel="telegram", dialog_id="dlg-a2")
CTX_B = ToolContext(user_id="user-b", channel="web", dialog_id="dlg-b")
CITY_KEY = "city"
CITY_CONTENT = "lives in Berlin"
SECOND_VERSION = 2
TOP_K = 10


class StubEmbedder:
    """EmbeddingClient stub returning the same vector for every text."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple((1.0, 0.0) for _ in texts)


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemyInstructionStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield SqlAlchemyInstructionStore(create_session_factory(engine))
    await engine.dispose()


@pytest.fixture
def service(store: SqlAlchemyInstructionStore) -> InstructionService:
    return LocalInstructionService(store, StubEmbedder())


@pytest.fixture
def store_tool(service: InstructionService) -> MemoryStoreTool:
    return MemoryStoreTool(service=service)


@pytest.fixture
def delete_tool(service: InstructionService) -> MemoryDeleteTool:
    return MemoryDeleteTool(service=service)


async def find_memory_keys(service: InstructionService, user_id: str) -> list[str]:
    hits = await service.search(user_id, "memories", TOP_K, InstructionType.MEMORY)
    return [hit.instruction.title for hit in hits]


# --- memory_store -----------------------------------------------------------


def test_store_tool_spec(store_tool: MemoryStoreTool) -> None:
    assert store_tool.spec.name == "memory_store"
    assert store_tool.spec.parameters_schema["required"] == ["key", "content"]


def test_tool_schemas_have_no_scope(
    store_tool: MemoryStoreTool, delete_tool: MemoryDeleteTool
) -> None:
    """The global write scope was removed: shared facts go through knowledge+publish."""
    assert "scope" not in store_tool.spec.parameters_schema["properties"]
    assert "scope" not in delete_tool.spec.parameters_schema["properties"]


async def test_store_creates_a_private_memory_record(
    service: InstructionService,
    store_tool: MemoryStoreTool,
) -> None:
    output = await store_tool.execute(
        {"key": CITY_KEY, "content": CITY_CONTENT, "tags": ["profile"]}, CTX_A
    )

    record = await service.get_by_name(CITY_KEY, InstructionType.MEMORY, CTX_A.user_id)
    assert output == "memory stored (key=city, version=1)"
    assert record.type is InstructionType.MEMORY
    assert record.owner_id == CTX_A.user_id
    assert record.content == CITY_CONTENT
    assert record.tags == ("profile",)


async def test_store_upserts_by_key(store_tool: MemoryStoreTool) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)
    updated = await store_tool.execute({"key": CITY_KEY, "content": "moved to Lisbon"}, CTX_A)

    assert updated == f"memory stored (key=city, version={SECOND_VERSION})"


async def test_memories_come_back_through_recall(
    service: InstructionService,
    store_tool: MemoryStoreTool,
) -> None:
    """The storage merge's point: one ranked search (recall) covers memories too."""
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    unfiltered = await service.search(CTX_A.user_id, "where does the user live", TOP_K)
    memory_only = await service.search(
        CTX_A.user_id, "where does the user live", TOP_K, InstructionType.MEMORY
    )

    assert CITY_KEY in [hit.instruction.title for hit in unfiltered]
    assert [hit.instruction.title for hit in memory_only] == [CITY_KEY]


async def test_store_writes_are_owner_scoped(
    service: InstructionService,
    store_tool: MemoryStoreTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    assert CITY_KEY in await find_memory_keys(service, CTX_A.user_id)
    assert CITY_KEY not in await find_memory_keys(service, CTX_B.user_id)


async def test_memories_visible_across_user_surfaces(
    service: InstructionService,
    store_tool: MemoryStoreTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    assert CITY_KEY in await find_memory_keys(service, CTX_A_OTHER_SURFACE.user_id)


async def test_store_rejects_invalid_arguments(store_tool: MemoryStoreTool) -> None:
    with pytest.raises(ToolArgumentsError):
        await store_tool.execute({"content": CITY_CONTENT}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await store_tool.execute({"key": "  ", "content": CITY_CONTENT}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await store_tool.execute({"key": CITY_KEY, "content": ""}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await store_tool.execute({"key": CITY_KEY, "content": 42}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT, "tags": "geo"}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await store_tool.execute(
            {"key": CITY_KEY, "content": CITY_CONTENT, "tags": ["geo", 1]}, CTX_A
        )


# --- memory_delete ----------------------------------------------------------


def test_delete_tool_spec(delete_tool: MemoryDeleteTool) -> None:
    assert delete_tool.spec.name == "memory_delete"
    assert delete_tool.spec.parameters_schema["required"] == ["key"]


async def test_delete_removes_memory(
    service: InstructionService,
    store_tool: MemoryStoreTool,
    delete_tool: MemoryDeleteTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    output = await delete_tool.execute({"key": CITY_KEY}, CTX_A)

    assert output == "memory 'city' deleted"
    assert CITY_KEY not in await find_memory_keys(service, CTX_A.user_id)


async def test_delete_missing_reports_text(delete_tool: MemoryDeleteTool) -> None:
    output = await delete_tool.execute({"key": "missing"}, CTX_A)

    assert output == "memory 'missing' not found"


async def test_delete_cannot_reach_legacy_global_memories(
    store: SqlAlchemyInstructionStore,
    delete_tool: MemoryDeleteTool,
) -> None:
    """Migrated global entries are public records, out of the agent tool's reach."""
    await store.upsert(
        InstructionDraft(
            kind=InstructionType.MEMORY,
            title="date_format",
            content="ISO",
            tags=(),
            embedding=(1.0, 0.0),
            owner_id=None,  # what the data migration produces for user_id NULL
        )
    )

    output = await delete_tool.execute({"key": "date_format"}, CTX_A)

    assert output == "memory 'date_format' not found"


async def test_delete_respects_owner_isolation(
    service: InstructionService,
    store_tool: MemoryStoreTool,
    delete_tool: MemoryDeleteTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    output = await delete_tool.execute({"key": CITY_KEY}, CTX_B)

    assert output == "memory 'city' not found"
    assert CITY_KEY in await find_memory_keys(service, CTX_A.user_id)


async def test_delete_rejects_invalid_arguments(delete_tool: MemoryDeleteTool) -> None:
    with pytest.raises(ToolArgumentsError):
        await delete_tool.execute({}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await delete_tool.execute({"key": "  "}, CTX_A)
