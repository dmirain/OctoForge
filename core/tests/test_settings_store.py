"""Operator settings: a generic key→value table, and the cap it carries first."""

from collections.abc import AsyncIterator

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.settings.api import (
    MAX_ACTIVE_USERS_KEY,
    InvalidSettingError,
    max_active_users,
)
from octoforge_core.settings.store import SqlAlchemySettingsStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
CAP = 25


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemySettingsStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield SqlAlchemySettingsStore(create_session_factory(engine))
    await engine.dispose()


async def test_put_get_replace_delete(store: SqlAlchemySettingsStore) -> None:
    assert await store.get("anything") is None

    await store.put(MAX_ACTIVE_USERS_KEY, "10")
    replaced = await store.put(MAX_ACTIVE_USERS_KEY, str(CAP))

    assert replaced.value == str(CAP)
    assert await store.get(MAX_ACTIVE_USERS_KEY) == str(CAP)
    assert [item.key for item in await store.list()] == [MAX_ACTIVE_USERS_KEY]

    await store.delete(MAX_ACTIVE_USERS_KEY)
    await store.delete(MAX_ACTIVE_USERS_KEY)  # absence is the goal, not an error
    assert await store.get(MAX_ACTIVE_USERS_KEY) is None


async def test_grammar_is_enforced(store: SqlAlchemySettingsStore) -> None:
    with pytest.raises(InvalidSettingError):
        await store.put("Bad Key!", "1")
    with pytest.raises(InvalidSettingError):
        await store.put("ok_key", "   ")


async def test_the_cap_reads_absence_as_no_cap(store: SqlAlchemySettingsStore) -> None:
    assert await max_active_users(store) is None

    await store.put(MAX_ACTIVE_USERS_KEY, str(CAP))
    assert await max_active_users(store) == CAP

    await store.put(MAX_ACTIVE_USERS_KEY, "0")  # the pause button, not "no cap"
    assert await max_active_users(store) == 0


async def test_a_corrupt_cap_fails_open(store: SqlAlchemySettingsStore) -> None:
    """A malformed row must not silently freeze registration for everyone."""
    await store.put(MAX_ACTIVE_USERS_KEY, "ten")
    assert await max_active_users(store) is None

    await store.put(MAX_ACTIVE_USERS_KEY, "-5")
    assert await max_active_users(store) is None
