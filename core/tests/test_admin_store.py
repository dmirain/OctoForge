"""Tests for the admin read model: cross-user listings, counters and paging."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin.api import MAX_PAGE_SIZE, clamp_page
from octoforge_core.admin.store import SqlAlchemyAdminStore
from octoforge_core.context.api import DialogueSummary
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.cron.api import CronJob
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.datasets.api import DatasetSchema
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.instructions.api import InstructionDraft, InstructionType
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import SqlAlchemyTaskStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "alice"
USER_B = "bob"
CHANNEL = "web"
TELEGRAM = "telegram"
EMBEDDING = (0.1, 0.2)
NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
PAGE_ONE = 1
TWO_ITEMS = 2
THREE_ITEMS = 3
OVERSIZED_LIMIT = 10_000
DEFAULT_LIMIT = 50


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyAdminStore:
    return SqlAlchemyAdminStore(session_factory)


def cron_job(job_id: str, user_id: str, next_fire_at: datetime) -> CronJob:
    return CronJob(
        id=job_id,
        user_id=user_id,
        channel=CHANNEL,
        title=f"job {job_id}",
        schedule="0 9 * * *",
        timezone="UTC",
        prompt="do it",
        enabled=True,
        next_fire_at=next_fire_at,
        last_fire_at=None,
        claimed_by=None,
        claimed_at=None,
        created_at=NOW,
        one_shot=False,
        last_status=None,
        last_error=None,
        retry_count=0,
    )


def task(dialog_id: str, status: TaskStatus, kind: TaskKind = TaskKind.RUN) -> Task:
    return Task(
        dialog_id=dialog_id,
        title=f"{kind.value}-{status.value}",
        kind=kind,
        input={"prompt": "x"},
        status=status,
        started_at=NOW,
    )


def memory_draft(key: str, owner_id: str | None) -> InstructionDraft:
    """A memory record as memory_store (or the data migration) produces it."""
    return InstructionDraft(
        kind=InstructionType.MEMORY,
        title=key,
        content=f"note about {key}",
        tags=(),
        embedding=EMBEDDING,
        owner_id=owner_id,
    )


def test_clamp_page_defaults_and_caps() -> None:
    assert clamp_page(None, None) == (DEFAULT_LIMIT, 0)
    assert clamp_page(OVERSIZED_LIMIT, None) == (MAX_PAGE_SIZE, 0)
    assert clamp_page(0, -5) == (PAGE_ONE, 0)  # a wire boundary may send anything


async def test_totals_counts_every_entity(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_A, CHANNEL)
    await SqlAlchemyMessageRepository(session_factory).append(
        dialog.id, ChatMessage(role=MessageRole.USER, content="hi")
    )
    await SqlAlchemyTaskStore(session_factory).add(task(dialog.id, TaskStatus.DONE))
    await SqlAlchemyCronStore(session_factory).create(cron_job("j1", USER_A, NOW))
    await SqlAlchemyInstructionStore(session_factory).upsert(memory_draft("diet", USER_A))
    await SqlAlchemyExchangeRepository(session_factory).create(dialog.id, "a question")

    totals = await store.totals()

    assert (totals.dialogs, totals.messages, totals.tasks) == (1, 1, 1)
    assert (totals.cron_jobs, totals.memories, totals.exchanges) == (1, 1, 1)
    assert (totals.instructions, totals.datasets, totals.dialog_summaries) == (0, 0, 0)


async def test_dialogs_carry_counters_and_newest_activity_first(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    first = await dialogs.get_or_create(USER_A, CHANNEL)
    second = await dialogs.get_or_create(USER_B, TELEGRAM)
    await messages.append(first.id, ChatMessage(role=MessageRole.USER, content="one"))
    await messages.append(second.id, ChatMessage(role=MessageRole.USER, content="two"))
    await messages.append(second.id, ChatMessage(role=MessageRole.ASSISTANT, content="three"))
    await SqlAlchemyTaskStore(session_factory).add(task(second.id, TaskStatus.RUNNING))

    page = await store.list_dialogs(DEFAULT_LIMIT, 0)

    assert page.total == TWO_ITEMS
    newest = page.items[0]
    assert newest.user_id == USER_B  # touched last
    assert (newest.user_message_count, newest.agent_message_count) == (1, 1)
    assert newest.task_count == 1
    assert newest.last_message_at is not None
    assert (page.items[1].user_message_count, page.items[1].agent_message_count) == (1, 0)


async def test_dialogs_split_the_users_last_day_of_writing(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    """ "Wrote today" and "last wrote" are about the person's own messages:
    agent replies move neither, and older writing falls out of the window."""
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_A, CHANNEL)
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="old"))
    async with session_factory() as session:  # push the first message out of the day window
        await session.execute(sql_update(MessageRow).values(created_at=NOW - timedelta(days=365)))
        await session.commit()
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="fresh"))
    await messages.append(dialog.id, ChatMessage(role=MessageRole.ASSISTANT, content="reply"))

    page = await store.list_dialogs(DEFAULT_LIMIT, 0)

    item = page.items[0]
    assert item.user_messages_24h == 1  # "old" left the window, the reply is not theirs
    assert item.last_user_message_at is not None
    assert item.last_message_at is not None
    assert item.last_user_message_at < item.last_message_at  # the reply came after


async def test_messages_are_paginated_by_seq(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_A, CHANNEL)
    messages = SqlAlchemyMessageRepository(session_factory)
    for index in range(3):
        await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content=f"m{index}"))

    first_page = await store.list_messages(dialog.id, 2, 0)
    second_page = await store.list_messages(dialog.id, 2, 2)

    assert first_page.total == THREE_ITEMS
    assert [item.seq for item in first_page.items] == [1, 2]
    assert [item.content for item in second_page.items] == ["m2"]
    assert second_page.offset == TWO_ITEMS


async def test_tasks_filter_by_status_and_kind(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_A, CHANNEL)
    tasks = SqlAlchemyTaskStore(session_factory)
    await tasks.add(task(dialog.id, TaskStatus.DONE))
    await tasks.add(task(dialog.id, TaskStatus.FAILED))
    await tasks.add(task(dialog.id, TaskStatus.DONE, kind=TaskKind.ANSWER))

    done = await store.list_tasks(DEFAULT_LIMIT, 0, status=TaskStatus.DONE.value)
    answers = await store.list_tasks(DEFAULT_LIMIT, 0, kind=TaskKind.ANSWER.value)
    everything = await store.list_tasks(DEFAULT_LIMIT, 0)

    assert done.total == TWO_ITEMS
    assert answers.total == 1
    assert everything.total == THREE_ITEMS


async def test_cron_jobs_of_every_user_next_firing_first(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    cron = SqlAlchemyCronStore(session_factory)
    await cron.create(cron_job("late", USER_A, NOW + timedelta(hours=2)))
    await cron.create(cron_job("soon", USER_B, NOW))

    page = await store.list_cron_jobs(DEFAULT_LIMIT, 0)

    assert [item.id for item in page.items] == ["soon", "late"]
    assert {item.user_id for item in page.items} == {USER_A, USER_B}


async def test_instructions_of_every_owner_and_title_search(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    instructions = SqlAlchemyInstructionStore(session_factory)
    for owner, title in ((None, "public_skill"), (USER_A, "alice_skill"), (USER_B, "bob_note")):
        await instructions.upsert(
            InstructionDraft(
                kind=InstructionType.SKILL,
                title=title,
                content="body",
                tags=(),
                embedding=EMBEDDING,
                owner_id=owner,
            )
        )

    everything = await store.list_instructions(DEFAULT_LIMIT, 0)
    matching = await store.list_instructions(DEFAULT_LIMIT, 0, query="SKILL")

    assert everything.total == THREE_ITEMS
    # the query is case-insensitive, so an uppercase needle still matches
    assert {item.title for item in matching.items} == {"public_skill", "alice_skill"}


async def test_datasets_and_their_records(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    datasets = SqlAlchemyDatasetStore(session_factory)
    dataset = await datasets.create(
        USER_A, "meals", "meal log", DatasetSchema(()), "", "", EMBEDDING
    )
    await datasets.add_record(dataset.id, USER_A, {"dish": "soup"})
    await datasets.add_record(dataset.id, USER_A, {"dish": "salad"})

    listed = await store.list_datasets(DEFAULT_LIMIT, 0)
    records = await store.list_dataset_records(dataset.id, DEFAULT_LIMIT, 0)

    assert [item.name for item in listed.items] == ["meals"]
    assert records.total == TWO_ITEMS
    assert {record.payload["dish"] for record in records.items} == {"soup", "salad"}


async def test_memories_include_the_legacy_global_scope(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    instructions = SqlAlchemyInstructionStore(session_factory)
    await instructions.upsert(memory_draft("diet", USER_A))
    await instructions.upsert(memory_draft("timezone", None))

    page = await store.list_memories(DEFAULT_LIMIT, 0)

    assert page.total == TWO_ITEMS
    assert {item.key for item in page.items} == {"diet", "timezone"}
    assert None in {item.user_id for item in page.items}


async def test_memories_and_instructions_tabs_split_one_table(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    """Memory rows must not leak into the instructions tab, nor vice versa."""
    instructions = SqlAlchemyInstructionStore(session_factory)
    await instructions.upsert(memory_draft("diet", USER_A))
    await instructions.upsert(
        InstructionDraft(
            kind=InstructionType.KNOWLEDGE,
            title="fact",
            content="shared",
            tags=(),
            embedding=EMBEDDING,
        )
    )

    listed = await store.list_instructions(DEFAULT_LIMIT, 0)
    memories = await store.list_memories(DEFAULT_LIMIT, 0)
    totals = await store.totals()

    assert [item.title for item in listed.items] == ["fact"]
    assert [item.key for item in memories.items] == ["diet"]
    assert (totals.instructions, totals.memories) == (1, 1)


async def test_summaries_are_listed_across_dialogs(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_A, CHANNEL)
    await SqlAlchemySummaryStore(session_factory).replace_for_dialog(
        dialog.id,
        DialogueSummary(
            id="s1",
            dialog_id=dialog.id,
            seq_from=1,
            seq_to=9,
            topics=("food",),
            content="compressed",
            created_at=NOW,
        ),
    )

    page = await store.list_summaries(DEFAULT_LIMIT, 0)

    assert page.total == 1
    assert page.items[0].topics == ("food",)


async def test_exchanges_join_dialog_identity_and_filter(
    session_factory: async_sessionmaker[AsyncSession],
    store: SqlAlchemyAdminStore,
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    alice = await dialogs.get_or_create(USER_A, CHANNEL)
    bob = await dialogs.get_or_create(USER_B, TELEGRAM)
    await exchanges.create(alice.id, "alice question")
    waiting = await exchanges.create(bob.id, "bob question", status=ExchangeStatus.IN_PROGRESS)
    await exchanges.set_status(
        waiting.id,
        ExchangeStatus.AWAITING_USER,
        pending_question="which one?",
    )

    everything = await store.list_exchanges(DEFAULT_LIMIT, 0)
    for_bob = await store.list_exchanges(DEFAULT_LIMIT, 0, user_id=USER_B)
    awaiting_only = await store.list_exchanges(
        DEFAULT_LIMIT, 0, status=ExchangeStatus.AWAITING_USER.value
    )

    assert everything.total == TWO_ITEMS
    assert {item.user_id for item in everything.items} == {USER_A, USER_B}
    # bob's exchange was set_status'd last, so it sorts first (updated_at desc)
    assert everything.items[0].dialog_id == bob.id
    assert for_bob.total == 1
    assert for_bob.items[0].channel == TELEGRAM
    assert awaiting_only.total == 1
    assert awaiting_only.items[0].pending_question == "which one?"
    assert awaiting_only.items[0].status == ExchangeStatus.AWAITING_USER
