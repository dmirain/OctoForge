"""Tests for the SQLAlchemy persistence layer on in-memory SQLite."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.runner import INTERRUPTED_NOTE
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.errors import DialogNotFoundError
from octoforge_core.db.models import MessageRow
from octoforge_core.db.repositories import (
    DialogRepository,
    MessageRepository,
    SqlAlchemyTaskStore,
)
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.usage import Usage
from octoforge_core.tasks.errors import TaskNotFoundError
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_ID = "user-1"
OTHER_USER_ID = "user-2"
CHANNEL = "web"
OTHER_CHANNEL = "telegram"
DIALOG_ID = "dlg-1"
OTHER_DIALOG_ID = "dlg-2"
TASK_TITLE = "research"
TASK_RESULT = "42"
TASK_ERROR = "boom"
EXPECTED_DIALOG_COUNT = 3
OWN_TASK_COUNT = 2
FIRST_SEQ = 1
SECOND_SEQ = 2
THIRD_SEQ = 3
PROMPT_TOKENS = 321
COMPLETION_TOKENS = 12
CLIENT_MESSAGE_ID = "client-key-1"
EXPECTED_UNKEYED_PLUS_ONE = 3
CREATED_EARLIER = datetime(2026, 1, 1, tzinfo=UTC)
TOOL_CALL = ToolCall(id="call-1", name="http_request", arguments={"url": "https://example.com"})


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def make_task(
    dialog_id: str = DIALOG_ID,
    user_id: str = USER_ID,
    title: str = TASK_TITLE,
    created_at: datetime = CREATED_EARLIER,
) -> Task:
    return Task(
        dialog_id=dialog_id,
        user_id=user_id,
        channel=CHANNEL,
        title=title,
        kind=TaskKind.RUN,
        input={"title": title, "prompt": "solve 2+2"},
        created_at=created_at,
    )


async def test_get_or_create_creates_then_reuses_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = DialogRepository(session_factory)

    first = await repo.get_or_create(USER_ID, CHANNEL)
    second = await repo.get_or_create(USER_ID, CHANNEL)

    assert first.id == second.id
    assert second.user_id == USER_ID
    assert second.channel == CHANNEL


async def test_dialogs_are_unique_per_user_and_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = DialogRepository(session_factory)

    base = await repo.get_or_create(USER_ID, CHANNEL)
    other_user = await repo.get_or_create(OTHER_USER_ID, CHANNEL)
    other_channel = await repo.get_or_create(USER_ID, OTHER_CHANNEL)

    assert len({base.id, other_user.id, other_channel.id}) == EXPECTED_DIALOG_COUNT


async def test_get_returns_dialog_by_id(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo = DialogRepository(session_factory)
    created = await repo.get_or_create(USER_ID, CHANNEL)

    fetched = await repo.get(created.id)

    assert fetched == created


async def test_get_unknown_dialog_raises(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo = DialogRepository(session_factory)

    with pytest.raises(DialogNotFoundError):
        await repo.get("missing")


async def test_dialog_timestamps_round_trip_as_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = DialogRepository(session_factory)
    created = await repo.get_or_create(USER_ID, CHANNEL)

    fetched = await repo.get(created.id)

    assert fetched.created_at.tzinfo == UTC
    assert fetched.updated_at.tzinfo == UTC


async def test_list_user_ids_by_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = DialogRepository(session_factory)
    await repo.get_or_create(USER_ID, CHANNEL)
    await repo.get_or_create(OTHER_USER_ID, CHANNEL)
    await repo.get_or_create(USER_ID, OTHER_CHANNEL)

    web_users = await repo.list_user_ids_by_channel(CHANNEL)
    telegram_users = await repo.list_user_ids_by_channel(OTHER_CHANNEL)

    assert sorted(web_users) == [USER_ID, OTHER_USER_ID]
    assert telegram_users == [USER_ID]


async def test_messages_get_monotonic_seq_and_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="one"))
    await messages.append(dialog.id, ChatMessage(role=MessageRole.ASSISTANT, content="two"))
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="three"))

    stored = await messages.list(dialog.id)
    assert [m.content for m in stored] == ["one", "two", "three"]
    assert [m.role for m in stored] == [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.USER]
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(MessageRow).where(MessageRow.dialog_id == dialog.id).order_by(MessageRow.seq)
            )
        ).all()
    assert [row.seq for row in rows] == [FIRST_SEQ, SECOND_SEQ, THIRD_SEQ]
    assert all(row.created_at.tzinfo == UTC for row in rows)


async def test_append_pair_writes_both_messages_in_one_transaction() -> None:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    commits = 0

    def _count_commit(_connection: object) -> None:
        nonlocal commits
        commits += 1

    event.listen(engine.sync_engine, "commit", _count_commit)
    try:
        factory = create_session_factory(engine)
        dialogs = DialogRepository(factory)
        messages = MessageRepository(factory)
        dialog = await dialogs.get_or_create(USER_ID, CHANNEL)
        await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="question"))
        commits_before = commits

        await messages.append_pair(
            dialog.id,
            ChatMessage(role=MessageRole.ASSISTANT, content="partial answer"),
            ChatMessage(role=MessageRole.SYSTEM, content=INTERRUPTED_NOTE),
        )

        assert commits == commits_before + 1  # one transaction for the whole pair
        stored = await messages.list(dialog.id)
        assert [m.content for m in stored] == ["question", "partial answer", INTERRUPTED_NOTE]
        async with factory() as session:
            rows = (
                await session.scalars(
                    select(MessageRow)
                    .where(MessageRow.dialog_id == dialog.id)
                    .order_by(MessageRow.seq)
                )
            ).all()
        assert [row.seq for row in rows] == [FIRST_SEQ, SECOND_SEQ, THIRD_SEQ]
    finally:
        await engine.dispose()


async def test_message_usage_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    await messages.append(
        dialog.id,
        ChatMessage(role=MessageRole.ASSISTANT, content="answer"),
        usage=Usage(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS),
    )
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="plain"))

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(MessageRow).where(MessageRow.dialog_id == dialog.id).order_by(MessageRow.seq)
            )
        ).all()
    assert rows[0].prompt_tokens == PROMPT_TOKENS
    assert rows[0].completion_tokens == COMPLETION_TOKENS
    assert rows[1].prompt_tokens is None
    assert rows[1].completion_tokens is None


async def test_client_message_id_dedup_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    assert not await messages.find_by_client_id(dialog.id, CLIENT_MESSAGE_ID)
    await messages.append(
        dialog.id,
        ChatMessage(role=MessageRole.USER, content="hi"),
        client_message_id=CLIENT_MESSAGE_ID,
    )
    assert await messages.find_by_client_id(dialog.id, CLIENT_MESSAGE_ID)

    with pytest.raises(IntegrityError):  # the unique constraint is the backstop
        await messages.append(
            dialog.id,
            ChatMessage(role=MessageRole.USER, content="hi again"),
            client_message_id=CLIENT_MESSAGE_ID,
        )

    # unkeyed messages coexist freely (NULLs are distinct in SQLite)
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="no key"))
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="no key 2"))
    assert len(await messages.list(dialog.id)) == EXPECTED_UNKEYED_PLUS_ONE


async def test_message_tool_calls_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    await messages.append(
        dialog.id,
        ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=(TOOL_CALL,)),
    )
    await messages.append(
        dialog.id,
        ChatMessage(role=MessageRole.TOOL, content="output", tool_call_id=TOOL_CALL.id),
    )

    stored = await messages.list(dialog.id)
    assert stored[0].tool_calls == (TOOL_CALL,)
    assert stored[0].tool_call_id is None
    assert stored[1].tool_calls == ()
    assert stored[1].tool_call_id == TOOL_CALL.id


async def test_messages_are_isolated_per_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    first = await dialogs.get_or_create(USER_ID, CHANNEL)
    second = await dialogs.get_or_create(OTHER_USER_ID, CHANNEL)

    await messages.append(first.id, ChatMessage(role=MessageRole.USER, content="private"))

    assert await messages.list(second.id) == []


async def test_task_add_and_get(session_factory: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()

    await store.add(task)
    stored = await store.get(task.id)

    assert stored.id == task.id
    assert stored.dialog_id == task.dialog_id
    assert stored.user_id == task.user_id
    assert stored.status is TaskStatus.PENDING
    assert stored.kind is TaskKind.RUN
    assert stored.result_delivered is False
    assert stored.created_at.tzinfo == UTC


async def test_get_unknown_task_raises(session_factory: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAlchemyTaskStore(session_factory)

    with pytest.raises(TaskNotFoundError):
        await store.get("missing")


async def test_task_list_scoped_by_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    await store.add(make_task(title="a"))
    await store.add(make_task(title="b"))
    await store.add(make_task(dialog_id=OTHER_DIALOG_ID, user_id=OTHER_USER_ID, title="c"))

    own = await store.list(DIALOG_ID)
    other = await store.list(OTHER_DIALOG_ID)

    assert [task.title for task in own] == ["a", "b"]
    assert len(own) == OWN_TASK_COUNT
    assert [task.title for task in other] == ["c"]


async def test_cancel_marks_task_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    await store.cancel(task.id)

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.CANCELLED
    assert stored.finished_at is not None
    assert stored.finished_at.tzinfo == UTC
    assert await store.is_cancelled(task.id)


async def test_cancel_unknown_task_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)

    with pytest.raises(TaskNotFoundError):
        await store.cancel("missing")


async def test_is_cancelled_false_for_missing_or_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)
    await store.mark_running(task)

    assert not await store.is_cancelled("missing")
    assert not await store.is_cancelled(task.id)


async def test_count_active_counts_pending_and_running_of_the_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    pending = make_task(title="pending")
    running = make_task(title="running")
    done = make_task(title="done")
    cancelled = make_task(title="cancelled")
    other = make_task(dialog_id=OTHER_DIALOG_ID, user_id=OTHER_USER_ID, title="other")
    for task in (pending, running, done, cancelled, other):
        await store.add(task)
    await store.mark_running(running)
    await store.mark_done(done, TASK_RESULT)
    await store.cancel(cancelled.id)

    assert await store.count_active(DIALOG_ID) == OWN_TASK_COUNT
    assert await store.count_active(OTHER_DIALOG_ID) == 1


async def test_mark_running_sets_status_and_started_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    await store.mark_running(task)

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.RUNNING
    assert stored.started_at is not None
    assert stored.started_at.tzinfo == UTC
    assert task.status is TaskStatus.RUNNING


async def test_mark_done_sets_result_and_finished_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    await store.mark_done(task, TASK_RESULT)

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.DONE
    assert stored.result == TASK_RESULT
    assert stored.finished_at is not None
    assert stored.finished_at.tzinfo == UTC
    assert task.result == TASK_RESULT


async def test_mark_failed_sets_error(session_factory: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    await store.mark_failed(task, TASK_ERROR)

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error == TASK_ERROR
    assert stored.finished_at is not None


async def test_mark_delivered_sets_flag(session_factory: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    await store.mark_delivered(task.id)

    assert (await store.get(task.id)).result_delivered is True


async def test_mark_delivered_unknown_task_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)

    with pytest.raises(TaskNotFoundError):
        await store.mark_delivered("missing")
