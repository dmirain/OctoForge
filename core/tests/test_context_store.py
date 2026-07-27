"""Contract-style tests for the context module store (summaries + archive reads)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context.api import ArchiveFilter, DialogueSummary
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.dialogs.store import DialogRepository
from octoforge_core.domain import MessageRole

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "user-a"
USER_B = "user-b"
CHANNEL = "web"
DAY_ONE = datetime(2026, 1, 10, 10, 0, tzinfo=UTC)
DAY_TWO = datetime(2026, 1, 11, 10, 0, tzinfo=UTC)
CREATED = datetime(2026, 1, 9, 12, 0, tzinfo=UTC)
TWO_MESSAGES = 2
MAX_COVERED_SEQ = 8


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemySummaryStore:
    return SqlAlchemySummaryStore(session_factory)


async def make_dialog(session_factory: async_sessionmaker[AsyncSession], user_id: str) -> str:
    dialog = await DialogRepository(session_factory).get_or_create(user_id, CHANNEL)
    return dialog.id


async def add_message(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
    seq: int,
    content: str,
    created_at: datetime = DAY_ONE,
) -> None:
    async with session_factory() as session:
        session.add(
            MessageRow(
                id=uuid.uuid4().hex,
                dialog_id=dialog_id,
                seq=seq,
                role=MessageRole.USER.value,
                content=content,
                created_at=created_at,
            )
        )
        await session.commit()


def make_summary(
    dialog_id: str,
    seq_from: int,
    seq_to: int,
    topics: tuple[str, ...] = ("travel",),
    content: str = "compressed segment",
) -> DialogueSummary:
    return DialogueSummary(
        id=uuid.uuid4().hex,
        dialog_id=dialog_id,
        seq_from=seq_from,
        seq_to=seq_to,
        topics=topics,
        content=content,
        created_at=CREATED,
    )


# --- summaries ---------------------------------------------------------------


async def test_create_and_list_roundtrip(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_id = await make_dialog(session_factory, USER_A)
    await store.create(make_summary(dialog_id, 3, 4, topics=("beta",), content="second"))
    await store.create(make_summary(dialog_id, 1, 2, topics=("alpha",), content="first"))

    summaries = await store.list_for_dialog(dialog_id)

    assert [(s.seq_from, s.seq_to) for s in summaries] == [(1, 2), (3, 4)]
    first = summaries[0]
    assert first.dialog_id == dialog_id
    assert first.topics == ("alpha",)
    assert first.content == "first"
    assert first.created_at == CREATED


async def test_max_seq_to_tracks_the_compaction_boundary(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_a = await make_dialog(session_factory, USER_A)
    dialog_b = await make_dialog(session_factory, USER_B)

    assert await store.max_seq_to(dialog_a) == 0
    await store.create(make_summary(dialog_a, 1, 5))
    await store.create(make_summary(dialog_a, 6, 8))

    assert await store.max_seq_to(dialog_a) == MAX_COVERED_SEQ
    assert await store.max_seq_to(dialog_b) == 0  # dialogs are isolated


async def test_find_by_topic_matches_case_insensitively_within_the_dialog(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_a = await make_dialog(session_factory, USER_A)
    dialog_b = await make_dialog(session_factory, USER_B)
    await store.create(make_summary(dialog_a, 1, 2, topics=("Travel", "food")))
    await store.create(make_summary(dialog_a, 3, 4, topics=("work",)))
    await store.create(make_summary(dialog_b, 1, 2, topics=("travel",)))

    hits = await store.find_by_topic(dialog_a, "travel")

    assert [(s.seq_from, s.seq_to) for s in hits] == [(1, 2)]
    assert await store.find_by_topic(dialog_a, "  ") == []
    assert await store.find_by_topic(dialog_a, "unknown") == []


# --- archive reads -----------------------------------------------------------


async def test_count_and_tail_after_the_boundary(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_id = await make_dialog(session_factory, USER_A)
    for seq in (1, 2, 3):
        await add_message(session_factory, dialog_id, seq, f"message {seq}")

    assert await store.count_after(dialog_id, 1) == TWO_MESSAGES
    tail = await store.tail_after(dialog_id, 1)
    assert [(m.seq, m.content) for m in tail] == [(2, "message 2"), (3, "message 3")]
    assert tail[0].role is MessageRole.USER
    assert tail[0].created_at == DAY_ONE
    assert await store.tail_after(dialog_id, 3) == []


async def test_search_matches_substring_case_insensitively(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_id = await make_dialog(session_factory, USER_A)
    await add_message(session_factory, dialog_id, 1, "we flew to Berlin")
    await add_message(session_factory, dialog_id, 2, "nothing here")
    await add_message(session_factory, dialog_id, 3, "BERLIN again")

    hits = await store.search(dialog_id, "berlin", limit=10)

    assert [m.seq for m in hits] == [1, 3]
    assert await store.search(dialog_id, "  ", limit=10) == []


async def test_search_restricts_to_seq_ranges(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_id = await make_dialog(session_factory, USER_A)
    for seq in (1, 2, 3, 4):
        await add_message(session_factory, dialog_id, seq, f"hit {seq}")

    hits = await store.search(
        dialog_id, "hit", filters=ArchiveFilter(seq_ranges=((2, 3),)), limit=10
    )

    assert [m.seq for m in hits] == [2, 3]
    empty = await store.search(dialog_id, "hit", filters=ArchiveFilter(seq_ranges=()), limit=10)
    assert empty == []


async def test_search_filters_by_date_range(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_id = await make_dialog(session_factory, USER_A)
    await add_message(session_factory, dialog_id, 1, "hit one", created_at=DAY_ONE)
    await add_message(session_factory, dialog_id, 2, "hit two", created_at=DAY_TWO)

    from_hits = await store.search(
        dialog_id, "hit", filters=ArchiveFilter(date_from=DAY_TWO), limit=10
    )
    to_hits = await store.search(dialog_id, "hit", filters=ArchiveFilter(date_to=DAY_TWO), limit=10)

    assert [m.seq for m in from_hits] == [2]
    assert [m.seq for m in to_hits] == [1]  # date_to is exclusive


async def test_search_limits_and_isolates_dialogs(
    store: SqlAlchemySummaryStore,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_a = await make_dialog(session_factory, USER_A)
    dialog_b = await make_dialog(session_factory, USER_B)
    for seq in (1, 2, 3):
        await add_message(session_factory, dialog_a, seq, f"hit {seq}")
    await add_message(session_factory, dialog_b, 1, "hit foreign")

    limited = await store.search(dialog_a, "hit", limit=2)
    foreign = await store.search(dialog_b, "hit", limit=10)

    assert [m.seq for m in limited] == [1, 2]
    assert [m.seq for m in foreign] == [1]
