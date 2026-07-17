"""Contract-style tests for MemoryStore implementations.

The suite talks to the port only (put/get/search/delete) and builds the store
under test through the `store_factory` fixture, so the same suite can later
validate an HTTP implementation of the protocol by swapping that one fixture.
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.memory.api import MemoryNotFoundError, MemoryStore
from octoforge_core.memory.store import SqlAlchemyMemoryStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

OWNER_A = "user-a"
OWNER_B = "user-b"
CITY_KEY = "city"
CITY_CONTENT = "lives in Berlin"
NAME_KEY = "name"
NAME_CONTENT = "the user is called Ada"
GLOBAL_KEY = "timezone_format"
GLOBAL_CONTENT = "render dates as ISO 8601"
TWO_HITS = 2
THREE_MEMORIES = 3

StoreFactory = Callable[[async_sessionmaker[AsyncSession]], MemoryStore]


@pytest.fixture
def store_factory() -> StoreFactory:
    """The implementation under test; swap to run the suite over another one."""
    return SqlAlchemyMemoryStore


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(
    store_factory: StoreFactory,
    session_factory: async_sessionmaker[AsyncSession],
) -> MemoryStore:
    return store_factory(session_factory)


async def test_put_creates_memory(store: MemoryStore) -> None:
    memory, created = await store.put(OWNER_A, CITY_KEY, CITY_CONTENT, ("profile", "geo"))

    assert created is True
    assert memory.id
    assert memory.user_id == OWNER_A
    assert memory.key == CITY_KEY
    assert memory.content == CITY_CONTENT
    assert memory.tags == ("profile", "geo")
    assert memory.created_at.tzinfo == UTC
    assert memory.updated_at.tzinfo == UTC


async def test_put_replaces_same_owner_key(store: MemoryStore) -> None:
    first, _ = await store.put(OWNER_A, CITY_KEY, CITY_CONTENT, ("profile",))

    updated, created = await store.put(OWNER_A, CITY_KEY, "moved to Lisbon", ("geo",))

    assert created is False
    assert updated.id == first.id
    assert updated.content == "moved to Lisbon"
    assert updated.tags == ("geo",)
    assert updated.created_at == first.created_at
    assert updated.updated_at > first.updated_at
    assert await store.get(OWNER_A, CITY_KEY) == updated


async def test_same_key_different_owners_coexist(store: MemoryStore) -> None:
    own, _ = await store.put(OWNER_A, CITY_KEY, CITY_CONTENT)
    other, _ = await store.put(OWNER_B, CITY_KEY, "lives in Madrid")

    assert own.id != other.id
    assert (await store.get(OWNER_A, CITY_KEY)).content == CITY_CONTENT
    assert (await store.get(OWNER_B, CITY_KEY)).content == "lives in Madrid"


async def test_tags_round_trip(store: MemoryStore) -> None:
    memory, _ = await store.put(OWNER_A, CITY_KEY, CITY_CONTENT, ("a", "b"))
    empty, _ = await store.put(OWNER_A, NAME_KEY, NAME_CONTENT)

    assert memory.tags == ("a", "b")
    assert (await store.get(OWNER_A, CITY_KEY)).tags == ("a", "b")
    assert empty.tags == ()
    assert (await store.get(OWNER_A, NAME_KEY)).tags == ()


async def test_get_missing_raises(store: MemoryStore) -> None:
    with pytest.raises(MemoryNotFoundError):
        await store.get(OWNER_A, "missing")


async def test_get_global_requires_global_owner(store: MemoryStore) -> None:
    await store.put(None, GLOBAL_KEY, GLOBAL_CONTENT)

    assert (await store.get(None, GLOBAL_KEY)).content == GLOBAL_CONTENT
    with pytest.raises(MemoryNotFoundError):
        await store.get(OWNER_A, GLOBAL_KEY)


async def test_owner_isolation(store: MemoryStore) -> None:
    await store.put(OWNER_A, CITY_KEY, CITY_CONTENT)

    with pytest.raises(MemoryNotFoundError):
        await store.get(OWNER_B, CITY_KEY)
    with pytest.raises(MemoryNotFoundError):
        await store.delete(OWNER_B, CITY_KEY)
    assert await store.search(OWNER_B, "berlin", limit=10) == []
    assert len(await store.search(OWNER_A, "berlin", limit=10)) == 1


async def test_global_memory_visible_to_everyone(store: MemoryStore) -> None:
    await store.put(None, GLOBAL_KEY, GLOBAL_CONTENT)
    await store.put(OWNER_A, CITY_KEY, CITY_CONTENT)

    hits_a = await store.search(OWNER_A, "ISO", limit=10)
    hits_b = await store.search(OWNER_B, "ISO", limit=10)

    assert [hit.key for hit in hits_a] == [GLOBAL_KEY]
    assert [hit.key for hit in hits_b] == [GLOBAL_KEY]
    assert hits_a[0].user_id is None


async def test_put_global_same_key_twice_keeps_one_row(store: MemoryStore) -> None:
    await store.put(None, GLOBAL_KEY, GLOBAL_CONTENT)
    _, created = await store.put(None, GLOBAL_KEY, "render dates as RFC 3339")

    assert created is False
    for owner in (OWNER_A, OWNER_B):
        hits = await store.search(owner, "dates", limit=10)
        assert [hit.key for hit in hits] == [GLOBAL_KEY]
    assert (await store.get(None, GLOBAL_KEY)).content == "render dates as RFC 3339"


async def test_delete_removes_memory(store: MemoryStore) -> None:
    await store.put(OWNER_A, CITY_KEY, CITY_CONTENT)

    await store.delete(OWNER_A, CITY_KEY)

    with pytest.raises(MemoryNotFoundError):
        await store.get(OWNER_A, CITY_KEY)


async def test_delete_missing_raises(store: MemoryStore) -> None:
    with pytest.raises(MemoryNotFoundError):
        await store.delete(OWNER_A, "missing")


async def test_search_matches_key_case_insensitively(store: MemoryStore) -> None:
    await store.put(OWNER_A, CITY_KEY, CITY_CONTENT)

    hits = await store.search(OWNER_A, "CITY", limit=10)

    assert [hit.key for hit in hits] == [CITY_KEY]


async def test_search_matches_content_case_insensitively(store: MemoryStore) -> None:
    await store.put(OWNER_A, CITY_KEY, CITY_CONTENT)

    hits = await store.search(OWNER_A, "berlin", limit=10)

    assert [hit.key for hit in hits] == [CITY_KEY]


async def test_search_treats_like_wildcards_literally(store: MemoryStore) -> None:
    await store.put(OWNER_A, "discount", "user has a 100% discount")
    await store.put(OWNER_A, "score", "scored 1000 points")

    hits = await store.search(OWNER_A, "100%", limit=10)

    assert [hit.key for hit in hits] == ["discount"]


async def test_search_newest_first(store: MemoryStore) -> None:
    await store.put(OWNER_A, "first", "common body")
    await store.put(OWNER_A, "second", "common body")

    hits = await store.search(OWNER_A, "common", limit=10)

    assert [hit.key for hit in hits] == ["second", "first"]


async def test_search_respects_limit(store: MemoryStore) -> None:
    for index in range(THREE_MEMORIES):
        await store.put(OWNER_A, f"key-{index}", "common body")

    hits = await store.search(OWNER_A, "common", limit=TWO_HITS)

    assert len(hits) == TWO_HITS


async def test_search_blank_query_short_circuits(store: MemoryStore) -> None:
    await store.put(OWNER_A, CITY_KEY, CITY_CONTENT)

    assert await store.search(OWNER_A, "   ", limit=10) == []
    assert await store.search(OWNER_A, "", limit=10) == []
