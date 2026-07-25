"""Tests for the memory runtime tools over the real SQL store."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.memory.store import SqlAlchemyMemoryStore
from octoforge_core.memory.tools import (
    NO_HITS_MESSAGE,
    MemoryDeleteTool,
    MemorySearchTool,
    MemoryStoreTool,
)
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
CTX_A = ToolContext(user_id="user-a", channel="web", dialog_id="dlg-a")
CTX_A_OTHER_SURFACE = ToolContext(user_id="user-a", channel="telegram", dialog_id="dlg-a2")
CTX_B = ToolContext(user_id="user-b", channel="web", dialog_id="dlg-b")
DEFAULT_LIMIT = 2
MAX_LIMIT = 4
THREE_MEMORIES = 3
SNIPPET_CHARS = 300
LONG_CONTENT = "x" * 400
CITY_KEY = "city"
CITY_CONTENT = "lives in Berlin"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyMemoryStore:
    return SqlAlchemyMemoryStore(session_factory)


@pytest.fixture
def store_tool(store: SqlAlchemyMemoryStore) -> MemoryStoreTool:
    return MemoryStoreTool(store=store)


@pytest.fixture
def search_tool(store: SqlAlchemyMemoryStore) -> MemorySearchTool:
    return MemorySearchTool(store=store, default_limit=DEFAULT_LIMIT, max_limit=MAX_LIMIT)


@pytest.fixture
def delete_tool(store: SqlAlchemyMemoryStore) -> MemoryDeleteTool:
    return MemoryDeleteTool(store=store)


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


async def test_store_reports_created_then_updated(store_tool: MemoryStoreTool) -> None:
    created = await store_tool.execute(
        {"key": CITY_KEY, "content": CITY_CONTENT, "tags": ["profile"]}, CTX_A
    )
    updated = await store_tool.execute({"key": CITY_KEY, "content": "moved to Lisbon"}, CTX_A)

    assert created == "memory stored (key=city, created=true)"
    assert updated == "memory stored (key=city, created=false)"


async def test_store_writes_are_owner_scoped(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    assert CITY_CONTENT in await search_tool.execute({"query": "berlin"}, CTX_A)
    assert await search_tool.execute({"query": "berlin"}, CTX_B) == NO_HITS_MESSAGE


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


# --- memory_search ----------------------------------------------------------


def test_search_tool_spec(search_tool: MemorySearchTool) -> None:
    assert search_tool.spec.name == "memory_search"
    # nothing is required: a query-less call is the catalog request
    assert "required" not in search_tool.spec.parameters_schema


async def test_search_formats_numbered_lines(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT, "tags": ["profile"]}, CTX_A)
    await store_tool.execute({"key": "diet", "content": "vegetarian"}, CTX_A)

    output = await search_tool.execute({"query": "e"}, CTX_A)

    assert output == (
        "1. [user] diet — vegetarian — tags: -\n2. [user] city — lives in Berlin — tags: profile"
    )


async def test_search_collapses_multiline_content(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": "line one\nline two"}, CTX_A)

    output = await search_tool.execute({"query": "line"}, CTX_A)

    assert output == "1. [user] city — line one line two — tags: -"


async def test_search_truncates_snippet(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": LONG_CONTENT}, CTX_A)

    output = await search_tool.execute({"query": "x"}, CTX_A)

    assert output == f"1. [user] city — {'x' * SNIPPET_CHARS} — tags: -"


async def test_search_no_hits(search_tool: MemorySearchTool) -> None:
    assert await search_tool.execute({"query": "nothing"}, CTX_A) == NO_HITS_MESSAGE


async def test_search_applies_default_limit(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    for index in range(THREE_MEMORIES):
        await store_tool.execute({"key": f"key-{index}", "content": "common"}, CTX_A)

    output = await search_tool.execute({"query": "common"}, CTX_A)

    assert len(output.splitlines()) == DEFAULT_LIMIT


async def test_search_explicit_limit_overrides_default(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    for index in range(THREE_MEMORIES):
        await store_tool.execute({"key": f"key-{index}", "content": "common"}, CTX_A)

    output = await search_tool.execute({"query": "common", "limit": THREE_MEMORIES}, CTX_A)

    assert len(output.splitlines()) == THREE_MEMORIES


async def test_search_without_a_query_lists_the_whole_memory(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    """The agent cannot guess a substring for a memory it has never seen."""
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)
    await store_tool.execute({"key": "diet", "content": "vegetarian"}, CTX_A)

    catalog = await search_tool.execute({}, CTX_A)
    blank = await search_tool.execute({"query": "   "}, CTX_A)

    assert CITY_KEY in catalog
    assert "diet" in catalog
    assert blank == catalog


async def test_search_catalog_stays_owner_scoped(
    store: SqlAlchemyMemoryStore,
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    """A query-less listing must not become a hole in the owner isolation."""
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)
    await store.put(None, "shared", "everyone")  # legacy global entry, readable by all

    catalog = await search_tool.execute({}, CTX_B)

    assert CITY_KEY not in catalog
    assert "shared" in catalog


async def test_search_catalog_respects_the_default_limit(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    for index in range(THREE_MEMORIES):
        await store_tool.execute({"key": f"key-{index}", "content": "body"}, CTX_A)

    catalog = await search_tool.execute({}, CTX_A)

    assert len(catalog.splitlines()) == DEFAULT_LIMIT


async def test_search_rejects_invalid_arguments(search_tool: MemorySearchTool) -> None:
    with pytest.raises(ToolArgumentsError):
        await search_tool.execute({"query": 1}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await search_tool.execute({"query": "x", "limit": 0}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await search_tool.execute({"query": "x", "limit": MAX_LIMIT + 1}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await search_tool.execute({"query": "x", "limit": "2"}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await search_tool.execute({"query": "x", "limit": True}, CTX_A)


async def test_search_visible_across_user_surfaces(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    output = await search_tool.execute({"query": "berlin"}, CTX_A_OTHER_SURFACE)

    assert CITY_KEY in output


# --- memory_delete ----------------------------------------------------------


def test_delete_tool_spec(delete_tool: MemoryDeleteTool) -> None:
    assert delete_tool.spec.name == "memory_delete"
    assert delete_tool.spec.parameters_schema["required"] == ["key"]


async def test_delete_removes_memory(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
    delete_tool: MemoryDeleteTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    output = await delete_tool.execute({"key": CITY_KEY}, CTX_A)

    assert output == "memory 'city' deleted"
    assert await search_tool.execute({"query": "berlin"}, CTX_A) == NO_HITS_MESSAGE


async def test_delete_missing_reports_text(delete_tool: MemoryDeleteTool) -> None:
    output = await delete_tool.execute({"key": "missing"}, CTX_A)

    assert output == "memory 'missing' not found"


async def test_delete_cannot_reach_global_memories(
    store: SqlAlchemyMemoryStore,
    delete_tool: MemoryDeleteTool,
) -> None:
    """Legacy global entries stay readable but are out of the agent tool's reach."""
    await store.put(None, "date_format", "ISO")

    output = await delete_tool.execute({"key": "date_format"}, CTX_A)

    assert output == "memory 'date_format' not found"


async def test_delete_respects_owner_isolation(
    store_tool: MemoryStoreTool,
    search_tool: MemorySearchTool,
    delete_tool: MemoryDeleteTool,
) -> None:
    await store_tool.execute({"key": CITY_KEY, "content": CITY_CONTENT}, CTX_A)

    output = await delete_tool.execute({"key": CITY_KEY}, CTX_B)

    assert output == "memory 'city' not found"
    assert CITY_CONTENT in await search_tool.execute({"query": "berlin"}, CTX_A)


async def test_delete_rejects_invalid_arguments(delete_tool: MemoryDeleteTool) -> None:
    with pytest.raises(ToolArgumentsError):
        await delete_tool.execute({}, CTX_A)
    with pytest.raises(ToolArgumentsError):
        await delete_tool.execute({"key": "  "}, CTX_A)
