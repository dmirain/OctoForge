"""Tests for the SQL implementation of the InstructionStore port."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import IntegrityError
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
    real_find = store._writer._find_draft
    missed = False

    async def find_with_race(
        session: AsyncSession,
        draft: InstructionDraft,
    ) -> InstructionRow | None:
        nonlocal missed
        row = await real_find(session, draft)
        if row is not None and not missed:
            missed = True
            return None
        return row

    monkeypatch.setattr(store._writer, "_find_draft", find_with_race)

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


USER_A = "user-a"
USER_B = "user-b"


def private_draft(owner: str, title: str = "alpha") -> InstructionDraft:
    return InstructionDraft(
        kind=InstructionType.SKILL,
        title=title,
        content="content",
        tags=(),
        embedding=(1.0, 0.0),
        owner_id=owner,
    )


async def test_same_title_private_records_coexist_for_two_owners(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyInstructionStore(session_factory)

    record_a = await store.upsert(private_draft(USER_A))
    record_b = await store.upsert(private_draft(USER_B))

    assert record_a.id != record_b.id
    assert (await store.get_by_title("alpha", InstructionType.SKILL, USER_A)) == record_a
    assert (await store.get_by_title("alpha", InstructionType.SKILL, USER_B)) == record_b
    # the public row with the same title is still free to take
    public = await store.upsert(make_draft())
    assert public.owner_id is None


async def test_list_with_embeddings_filters_visibility(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyInstructionStore(session_factory)
    await store.upsert(make_draft())  # public
    await store.upsert(private_draft(USER_A, title="a-private"))
    await store.upsert(private_draft(USER_B, title="b-private"))

    visible_titles = {item.instruction.title for item in await store.list_with_embeddings(USER_A)}
    assert visible_titles == {"alpha", "a-private"}
    all_titles = {item.instruction.title for item in await store.list_with_embeddings(None)}
    assert all_titles == {"alpha", "a-private", "b-private"}


async def test_delete_by_id_is_scoped_to_the_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyInstructionStore(session_factory)
    record = await store.upsert(private_draft(USER_A))

    assert await store.delete_by_id(record.id, USER_B) is False  # not the owner
    assert await store.delete_by_id(record.id, USER_A) is True
    assert await store.get_by_title("alpha", InstructionType.SKILL, USER_A) is None


async def test_delete_by_id_never_matches_a_public_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyInstructionStore(session_factory)
    record = await store.upsert(make_draft())

    assert await store.delete_by_id(record.id, USER_A) is False


async def test_publish_clears_the_owner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyInstructionStore(session_factory)
    record = await store.upsert(private_draft(USER_A))

    published = await store.publish(record.id)

    assert published is not None
    assert published.owner_id is None
    assert await store.get_by_title("alpha", InstructionType.SKILL, USER_A) is None
    assert (await store.get_by_title("alpha", InstructionType.SKILL)).id == record.id
    assert await store.publish("missing-id") is None


async def test_publish_of_two_same_titled_records_conflicts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyInstructionStore(session_factory)
    await store.upsert(make_draft())  # public "alpha" already taken
    private = await store.upsert(private_draft(USER_A))

    with pytest.raises(IntegrityError):
        await store.publish(private.id)
