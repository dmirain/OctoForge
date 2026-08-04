"""Tests for the per-user params store."""

from collections.abc import AsyncIterator

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.params.api import InvalidUserParamError, UserParamNotFoundError
from octoforge_core.params.store import SqlAlchemyUserParamStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "person-1"
USER_B = "person-2"


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemyUserParamStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield SqlAlchemyUserParamStore(create_session_factory(engine))
    await engine.dispose()


async def test_roundtrip_and_replacement(store: SqlAlchemyUserParamStore) -> None:
    await store.put(USER_A, "timezone", "Europe/Berlin")
    await store.put(USER_A, "timezone", "Asia/Tbilisi")
    await store.put(USER_A, "account_id", "acc-1")

    values = await store.get_for_user(USER_A)
    listed = await store.list(USER_A)

    assert values == {"timezone": "Asia/Tbilisi", "account_id": "acc-1"}
    assert [param.code for param in listed] == ["account_id", "timezone"]


async def test_owner_isolation(store: SqlAlchemyUserParamStore) -> None:
    await store.put(USER_A, "timezone", "Europe/Berlin")

    assert await store.get_for_user(USER_B) == {}
    assert await store.list(USER_B) == []


async def test_delete(store: SqlAlchemyUserParamStore) -> None:
    await store.put(USER_A, "timezone", "Europe/Berlin")
    await store.delete(USER_A, "timezone")

    assert await store.get_for_user(USER_A) == {}
    with pytest.raises(UserParamNotFoundError):
        await store.delete(USER_A, "timezone")


async def test_validation(store: SqlAlchemyUserParamStore) -> None:
    with pytest.raises(InvalidUserParamError):
        await store.put(USER_A, "Bad Code!", "x")
    with pytest.raises(InvalidUserParamError):
        await store.put(USER_A, "timezone", "")
    with pytest.raises(InvalidUserParamError):  # header/URL injection guard
        await store.put(USER_A, "timezone", "value\r\nX-Evil: 1")
    # non-ASCII values are legal: they may go into a request body
    await store.put(USER_A, "city_name", "Тбилиси")
    assert (await store.get_for_user(USER_A))["city_name"] == "Тбилиси"
