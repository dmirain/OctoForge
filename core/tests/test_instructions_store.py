"""Tests for the SQL implementation of the InstructionStore port."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.instructions.api import InstructionDraft, InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.store import SqlAlchemyInstructionStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
FIRST_VERSION = 1
SECOND_VERSION = 2
THIRD_VERSION = 3


def make_draft(content: str = "content", system: bool = False) -> InstructionDraft:
    return InstructionDraft(
        kind=InstructionType.SKILL,
        title="alpha",
        content=content,
        tags=("t",),
        embedding=(1.0, 0.0),
        system=system,
    )


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


async def test_upsert_recovers_from_a_concurrent_insert_race(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the winner commits first; the loser's find reports a miss (the row was
    # committed between the find and the insert — what two instances syncing
    # the system registry over one SQLite file can produce at startup), so
    # the insert hits the real unique constraint
    winner = SqlAlchemyInstructionStore(session_factory)
    await winner.upsert(make_draft(content="winner content", system=True))
    store = SqlAlchemyInstructionStore(session_factory)
    real_find = store._find_row
    missed = False

    async def find_with_race(
        session: AsyncSession,
        kind: InstructionType,
        title: str,
    ) -> InstructionRow | None:
        nonlocal missed
        row = await real_find(session, kind, title)
        if row is not None and not missed:
            missed = True
            return None
        return row

    monkeypatch.setattr(store, "_find_row", find_with_race)

    stored = await store.upsert(make_draft(content="loser content", system=True))

    # the loser's insert became an update of the winner's row: no error escapes
    assert stored.content == "loser content"
    assert stored.system is True
    assert stored.version == SECOND_VERSION
    assert stored.tags == ("t",)

    # the store keeps working normally afterwards
    again = await store.upsert(make_draft(content="third content"))
    assert again.version == THIRD_VERSION
    assert again.content == "third content"


async def test_upsert_insert_and_update_paths(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyInstructionStore(session_factory)

    created = await store.upsert(make_draft())
    updated = await store.upsert(make_draft(content="new content"))

    assert created.version == FIRST_VERSION
    assert updated.id == created.id
    assert updated.version == SECOND_VERSION
    assert updated.content == "new content"
