"""Tests for the SQLAlchemy persistence layer on in-memory SQLite."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context.api import INTERRUPTED_NOTE
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.dialogs.api import (
    TITLE_MAX_LENGTH,
    DialogNotFoundError,
    DialogRepository,
    ExchangeNotFoundError,
    ExchangeStatus,
    MessageRepository,
)
from octoforge_core.dialogs.models import ExchangeRow, MessageRow
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ChatMessage, MessageKind, MessageRole, ToolCall
from octoforge_core.llm.usage import Usage
from octoforge_core.tasks.api import Task, TaskKind, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.store import SqlAlchemyTaskStore
from octoforge_core.time import utc_now

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
EXPECTED_RETRY_COMMIT_ATTEMPTS = 2
CREATED_EARLIER = datetime(2026, 1, 1, tzinfo=UTC)
TOOL_CALL = ToolCall(id="call-1", name="http_request", arguments={"url": "https://example.com"})
EXCHANGE_TITLE = "the budget report"
OWNER_TASK_ID = "task-1"
OTHER_OWNER_TASK_ID = "task-2"
PENDING_QUESTION = "which quarter?"
OTHER_PENDING_QUESTION = "which city?"
EXPECTED_LIVE_EXCHANGE_COUNT = 3
EXPECTED_REOPENED_COUNT = 2
COLLECTING_TITLE = "forwarded material"
STALE_QUIET_SECONDS = 30.0


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
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
    return Task(
        dialog_id=dialog_id,
        user_id=user_id,
        channel=CHANNEL,
        title=title,
        kind=TaskKind.RUN,
        input={"title": title, "prompt": "solve 2+2"},
        created_at=created_at,
        status=status,
    )


async def test_get_or_create_creates_then_reuses_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyDialogRepository(session_factory)

    first = await repo.get_or_create(USER_ID, CHANNEL)
    second = await repo.get_or_create(USER_ID, CHANNEL)

    assert first.id == second.id
    assert second.user_id == USER_ID
    assert second.channel == CHANNEL


async def test_dialogs_are_unique_per_user_and_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyDialogRepository(session_factory)

    base = await repo.get_or_create(USER_ID, CHANNEL)
    other_user = await repo.get_or_create(OTHER_USER_ID, CHANNEL)
    other_channel = await repo.get_or_create(USER_ID, OTHER_CHANNEL)

    assert len({base.id, other_user.id, other_channel.id}) == EXPECTED_DIALOG_COUNT


async def test_get_returns_dialog_by_id(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo = SqlAlchemyDialogRepository(session_factory)
    created = await repo.get_or_create(USER_ID, CHANNEL)

    fetched = await repo.get(created.id)

    assert fetched == created


async def test_get_unknown_dialog_raises(session_factory: async_sessionmaker[AsyncSession]) -> None:
    repo = SqlAlchemyDialogRepository(session_factory)

    with pytest.raises(DialogNotFoundError):
        await repo.get("missing")


async def test_dialog_timestamps_round_trip_as_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyDialogRepository(session_factory)
    created = await repo.get_or_create(USER_ID, CHANNEL)

    fetched = await repo.get(created.id)

    assert fetched.created_at.tzinfo == UTC
    assert fetched.updated_at.tzinfo == UTC


async def test_list_by_channel_returns_full_dialogs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyDialogRepository(session_factory)
    first = await repo.get_or_create(USER_ID, CHANNEL)
    second = await repo.get_or_create(OTHER_USER_ID, CHANNEL)
    await repo.get_or_create(USER_ID, OTHER_CHANNEL)

    dialogs = await repo.list_by_channel(CHANNEL)

    assert {dialog.id for dialog in dialogs} == {first.id, second.id}
    assert all(dialog.updated_at.tzinfo == UTC for dialog in dialogs)


async def test_appending_a_message_does_not_write_the_dialog_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The answer path must not touch `dialogs`.

    It used to, twice a turn, to keep `updated_at` current for two operator
    listings — a write nobody on the answer path needed. Both listings read
    the message log instead now.
    """
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="hi"))
    await messages.append(dialog.id, ChatMessage(role=MessageRole.ASSISTANT, content="hello"))

    assert (await dialogs.get(dialog.id)).updated_at == dialog.updated_at


async def test_last_activity_reads_the_message_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per-person activity of a channel, taken from what actually happened."""
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    first = await dialogs.get_or_create(USER_ID, CHANNEL)
    second = await dialogs.get_or_create(OTHER_USER_ID, CHANNEL)
    elsewhere = await dialogs.get_or_create(USER_ID, OTHER_CHANNEL)
    await messages.append(first.id, ChatMessage(role=MessageRole.USER, content="one"))
    await messages.append(elsewhere.id, ChatMessage(role=MessageRole.USER, content="other channel"))

    activity = await messages.last_activity_by_channel(CHANNEL)

    assert set(activity) == {USER_ID}  # the silent dialog has no entry, the other channel none
    assert activity[USER_ID].tzinfo == UTC
    assert second.user_id == OTHER_USER_ID  # created, never wrote


async def test_message_stats_by_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    first = await dialogs.get_or_create(USER_ID, CHANNEL)
    second = await dialogs.get_or_create(OTHER_USER_ID, CHANNEL)
    other_channel = await dialogs.get_or_create(USER_ID, OTHER_CHANNEL)
    await messages.append(first.id, ChatMessage(role=MessageRole.USER, content="one"))
    await messages.append(first.id, ChatMessage(role=MessageRole.ASSISTANT, content="three"))
    await messages.append(second.id, ChatMessage(role=MessageRole.USER, content="four"))
    await messages.append(other_channel.id, ChatMessage(role=MessageRole.USER, content="ignored"))

    stats = {entry.user_id: entry for entry in await messages.stats_by_channel(CHANNEL)}

    assert stats[USER_ID].user_messages == 1
    assert stats[USER_ID].user_chars == len("one")
    assert stats[USER_ID].agent_messages == 1
    assert stats[USER_ID].agent_chars == len("three")
    assert stats[OTHER_USER_ID].user_messages == 1
    assert stats[OTHER_USER_ID].user_chars == len("four")
    assert stats[OTHER_USER_ID].agent_messages == 0


async def test_messages_get_monotonic_seq_and_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
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


async def test_append_returns_and_round_trips_the_row_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    message_id = await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="one"))

    (stored,) = await messages.list(dialog.id)
    assert stored.id == message_id
    # the row id is pure DB metadata: excluded from equality
    assert stored == ChatMessage(role=MessageRole.USER, content="one")


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
        dialogs = SqlAlchemyDialogRepository(factory)
        messages = SqlAlchemyMessageRepository(factory)
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
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
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


async def test_message_task_id_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    await messages.append(
        dialog.id,
        ChatMessage(role=MessageRole.ASSISTANT, content="task answer", task_id="task-1"),
    )
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="plain"))

    stored = await messages.list(dialog.id)
    assert stored[0].task_id == "task-1"
    assert stored[1].task_id is None
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(MessageRow).where(MessageRow.dialog_id == dialog.id).order_by(MessageRow.seq)
            )
        ).all()
    assert rows[0].task_id == "task-1"
    assert rows[1].task_id is None


async def test_client_message_id_dedup_round_trip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
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
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
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


async def test_append_retries_a_transient_seq_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost seq race (two writers reading the same MAX before either commits)
    surfaces as an IntegrityError on the loser's commit; `append` must retry
    with a freshly recomputed seq rather than lose the message.
    """
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    original_commit = AsyncSession.commit
    calls = 0

    async def flaky_commit(self: AsyncSession) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError("insert", {}, Exception("seq collision"))
        await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)

    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="hello"))

    assert calls == EXPECTED_RETRY_COMMIT_ATTEMPTS  # lost the race, then the retry succeeded
    stored = await messages.list(dialog.id)
    assert [m.content for m in stored] == ["hello"]


async def test_append_pair_retries_a_transient_seq_conflict(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)

    original_commit = AsyncSession.commit
    calls = 0

    async def flaky_commit(self: AsyncSession) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise IntegrityError("insert", {}, Exception("seq collision"))
        await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)

    await messages.append_pair(
        dialog.id,
        ChatMessage(role=MessageRole.ASSISTANT, content="partial answer"),
        ChatMessage(role=MessageRole.SYSTEM, content=INTERRUPTED_NOTE),
    )

    assert calls == EXPECTED_RETRY_COMMIT_ATTEMPTS
    stored = await messages.list(dialog.id)
    assert [m.content for m in stored] == ["partial answer", INTERRUPTED_NOTE]


async def test_messages_are_isolated_per_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
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


async def test_task_delete_removes_the_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    await store.delete(task.id)

    with pytest.raises(TaskNotFoundError):
        await store.get(task.id)


async def test_task_delete_unknown_task_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)

    with pytest.raises(TaskNotFoundError):
        await store.delete("missing")


async def test_mark_done_sets_result_and_finished_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    returned = await store.mark_done(task.id, TASK_RESULT)

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.DONE
    assert stored.result == TASK_RESULT
    assert stored.finished_at is not None
    assert stored.finished_at.tzinfo == UTC
    # the write hands the row back, so no caller needs a read to see it
    assert returned.result == TASK_RESULT
    assert returned.status is TaskStatus.DONE
    assert returned.delivered_at is None


async def test_mark_failed_sets_error(session_factory: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    await store.mark_failed(task.id, TASK_ERROR)

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error == TASK_ERROR
    assert stored.finished_at is not None


async def test_delivery_can_be_stamped_by_the_finishing_write(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An answer the user watched stream is delivered the moment it is done.

    Stamping it in the same statement is what removes the second write — and
    it is only correct because the caller asks for it exactly when delivery
    is already certain (see `_delivery_is_certain`).
    """
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task()
    await store.add(task)

    returned = await store.mark_done(task.id, TASK_RESULT, delivered=True)

    stored = await store.get(task.id)
    assert stored.delivered_at is not None
    assert returned.delivered_at == stored.delivered_at


async def test_list_orphaned_returns_pending_and_running_without_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    pending = make_task(title="pending")
    running = make_task(title="running", status=TaskStatus.RUNNING)
    done = make_task(title="done", status=TaskStatus.DONE)
    cancelled = make_task(title="cancelled", status=TaskStatus.CANCELLED)
    for task in (pending, running, done, cancelled):
        await store.add(task)

    orphaned = await store.list_orphaned()

    assert {task.id for task in orphaned} == {pending.id, running.id}
    # read-only: the sweep must not mutate the rows it returns
    assert (await store.get(pending.id)).status is TaskStatus.PENDING
    assert (await store.get(running.id)).status is TaskStatus.RUNNING


async def test_list_orphaned_without_candidates_returns_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    await store.add(make_task(status=TaskStatus.DONE))

    assert await store.list_orphaned() == []


async def test_list_undelivered_returns_terminal_rows_without_delivery_stamp(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    done = make_task(title="done", status=TaskStatus.DONE)
    failed = make_task(title="failed", status=TaskStatus.FAILED)
    delivered = make_task(title="delivered", status=TaskStatus.DONE)
    running = make_task(title="running", status=TaskStatus.RUNNING)
    for task in (done, failed, delivered, running):
        await store.add(task)
    await store.mark_delivered(delivered.id)

    undelivered = await store.list_undelivered()

    assert {task.id for task in undelivered} == {done.id, failed.id}


async def test_mark_delivered_stamps_the_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    task = make_task(status=TaskStatus.DONE)
    await store.add(task)

    await store.mark_delivered(task.id)

    stored = await store.get(task.id)
    assert stored.delivered_at is not None
    assert stored.delivered_at.tzinfo == UTC
    assert await store.list_undelivered() == []


async def test_mark_delivered_unknown_task_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)

    with pytest.raises(TaskNotFoundError):
        await store.mark_delivered("missing")


async def test_delete_removes_the_dialog_and_its_messages(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)
    other = await dialogs.get_or_create(OTHER_USER_ID, CHANNEL)
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="hi"))
    await messages.append(other.id, ChatMessage(role=MessageRole.USER, content="keep"))

    await dialogs.delete(dialog.id)

    with pytest.raises(DialogNotFoundError):
        await dialogs.get(dialog.id)
    assert await messages.list(dialog.id) == []
    # the neighbour dialog and its log survived
    assert [m.content for m in await messages.list(other.id)] == ["keep"]


async def test_delete_unknown_dialog_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(DialogNotFoundError):
        await SqlAlchemyDialogRepository(session_factory).delete("missing")


async def test_delete_for_dialog_removes_only_that_dialogs_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    await store.add(make_task())
    await store.add(make_task(title="second"))
    await store.add(make_task(dialog_id=OTHER_DIALOG_ID, title="keep"))

    removed = await store.delete_for_dialog(DIALOG_ID)

    assert removed == OWN_TASK_COUNT
    assert await store.list(DIALOG_ID) == []
    assert [task.title for task in await store.list(OTHER_DIALOG_ID)] == ["keep"]


async def _insert_exchange(  # noqa: PLR0913 — mirrors the ExchangeRow columns it sets
    session_factory: async_sessionmaker[AsyncSession],
    *,
    dialog_id: str = DIALOG_ID,
    title: str = EXCHANGE_TITLE,
    status: ExchangeStatus,
    created_at: datetime,
    updated_at: datetime | None = None,
    pending_question: str | None = None,
) -> str:
    """Insert an ExchangeRow directly, controlling `created_at`/`updated_at`.

    `updated_at` defaults to `created_at`: ordering tests only care about the
    latter, while the staleness tests (`list_stale_collecting`) need to
    backdate the former independently.
    """
    row_id = uuid.uuid4().hex
    async with session_factory() as session:
        session.add(
            ExchangeRow(
                id=row_id,
                dialog_id=dialog_id,
                status=status.value,
                title=title,
                pending_question=pending_question,
                created_at=created_at,
                updated_at=updated_at if updated_at is not None else created_at,
            )
        )
        await session.commit()
    return row_id


# --- SqlAlchemyExchangeRepository ------------------------------------------


async def test_exchange_create_defaults_to_open_and_unowned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)

    exchange = await repo.create(DIALOG_ID, EXCHANGE_TITLE)

    assert exchange.status is ExchangeStatus.OPEN
    assert exchange.pending_question is None
    assert exchange.title == EXCHANGE_TITLE
    assert exchange.created_at.tzinfo == UTC
    assert exchange.updated_at.tzinfo == UTC


async def test_exchange_create_with_an_explicit_status_starts_there(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)

    exchange = await repo.create(DIALOG_ID, EXCHANGE_TITLE, status=ExchangeStatus.IN_PROGRESS)

    assert exchange.status is ExchangeStatus.IN_PROGRESS


async def test_exchange_get_returns_the_created_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    created = await repo.create(DIALOG_ID, EXCHANGE_TITLE)

    fetched = await repo.get(created.id)

    assert fetched == created


async def test_exchange_get_unknown_id_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)

    with pytest.raises(ExchangeNotFoundError):
        await repo.get("missing")


async def test_exchange_list_live_excludes_terminal_statuses_and_orders_oldest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    open_id = await _insert_exchange(
        session_factory,
        title="open",
        status=ExchangeStatus.OPEN,
        created_at=CREATED_EARLIER,
    )
    in_progress_id = await _insert_exchange(
        session_factory,
        title="in progress",
        status=ExchangeStatus.IN_PROGRESS,
        created_at=CREATED_EARLIER + timedelta(seconds=1),
    )
    awaiting_id = await _insert_exchange(
        session_factory,
        title="awaiting",
        status=ExchangeStatus.AWAITING_USER,
        created_at=CREATED_EARLIER + timedelta(seconds=2),
    )
    for terminal_title, status, offset in (
        ("answered", ExchangeStatus.ANSWERED, 3),
        ("cancelled", ExchangeStatus.CANCELLED, 4),
        ("failed", ExchangeStatus.FAILED, 5),
    ):
        await _insert_exchange(
            session_factory,
            title=terminal_title,
            status=status,
            created_at=CREATED_EARLIER + timedelta(seconds=offset),
        )
    repo = SqlAlchemyExchangeRepository(session_factory)

    live = await repo.list_live(DIALOG_ID)

    assert len(live) == EXPECTED_LIVE_EXCHANGE_COUNT
    assert [item.id for item in live] == [open_id, in_progress_id, awaiting_id]


async def test_exchange_set_status_clears_the_question_when_omitted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    exchange = await repo.create(DIALOG_ID, EXCHANGE_TITLE, status=ExchangeStatus.IN_PROGRESS)

    await repo.set_status(
        exchange.id, ExchangeStatus.AWAITING_USER, pending_question=PENDING_QUESTION
    )
    parked = await repo.get(exchange.id)
    assert parked.status is ExchangeStatus.AWAITING_USER
    assert parked.pending_question == PENDING_QUESTION

    # the question is omitted on resume: it must be cleared, not remembered
    await repo.set_status(exchange.id, ExchangeStatus.IN_PROGRESS)
    resumed = await repo.get(exchange.id)
    assert resumed.status is ExchangeStatus.IN_PROGRESS
    assert resumed.pending_question is None


async def test_exchange_set_status_unknown_id_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)

    with pytest.raises(ExchangeNotFoundError):
        await repo.set_status("missing", ExchangeStatus.ANSWERED)


async def test_an_open_exchange_with_a_live_task_is_not_unowned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "Unowned" is derived from the tasks: a run in PENDING/RUNNING owns its
    exchange, a finished one does not. The window this closes is real — a
    task is created before the exchange leaves OPEN, and the sweep must not
    treat that instant as abandonment and spawn a second run.
    """
    repo = SqlAlchemyExchangeRepository(session_factory)
    tasks = SqlAlchemyTaskStore(session_factory)
    owned = await repo.create(DIALOG_ID, "being started right now")
    abandoned = await repo.create(DIALOG_ID, "its run is long done")
    for exchange, status in ((owned, TaskStatus.RUNNING), (abandoned, TaskStatus.DONE)):
        await tasks.add(
            Task(
                dialog_id=DIALOG_ID,
                user_id=USER_ID,
                channel=CHANNEL,
                title=exchange.title,
                kind=TaskKind.ANSWER,
                exchange_id=exchange.id,
                input={"prompt": exchange.title},
                status=status,
            )
        )

    unowned = await repo.list_unowned_open()
    stranded = await repo.list_stranded_dialog_ids()

    assert [item.id for item in unowned] == [abandoned.id]
    assert stranded == [DIALOG_ID]  # via the abandoned one only


async def test_exchange_reopen_in_progress_resets_only_those_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    open_exchange = await repo.create(DIALOG_ID, "open")
    stranded_one = await repo.create(DIALOG_ID, "stranded one", status=ExchangeStatus.IN_PROGRESS)
    stranded_two = await repo.create(DIALOG_ID, "stranded two", status=ExchangeStatus.IN_PROGRESS)
    awaiting = await repo.create(DIALOG_ID, "awaiting")
    await repo.set_status(
        awaiting.id, ExchangeStatus.AWAITING_USER, pending_question=OTHER_PENDING_QUESTION
    )

    reopened_count = await repo.reopen_in_progress(DIALOG_ID)

    assert reopened_count == EXPECTED_REOPENED_COUNT
    for exchange_id in (stranded_one.id, stranded_two.id):
        assert (await repo.get(exchange_id)).status is ExchangeStatus.OPEN
    # untouched rows keep their status and fields
    assert (await repo.get(open_exchange.id)).status is ExchangeStatus.OPEN
    still_awaiting = await repo.get(awaiting.id)
    assert still_awaiting.status is ExchangeStatus.AWAITING_USER
    assert still_awaiting.pending_question == OTHER_PENDING_QUESTION


async def test_exchange_reopen_in_progress_never_reaches_another_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scope is the whole point: with more than one process alive, a
    global reset would reopen exchanges a peer is answering right now."""
    repo = SqlAlchemyExchangeRepository(session_factory)
    mine = await repo.create(DIALOG_ID, "mine", status=ExchangeStatus.IN_PROGRESS)
    theirs = await repo.create(OTHER_DIALOG_ID, "theirs", status=ExchangeStatus.IN_PROGRESS)

    reopened_count = await repo.reopen_in_progress(DIALOG_ID)

    assert reopened_count == 1
    assert (await repo.get(mine.id)).status is ExchangeStatus.OPEN
    untouched = await repo.get(theirs.id)
    assert untouched.status is ExchangeStatus.IN_PROGRESS


async def test_exchange_delete_for_dialog_scopes_to_one_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    own = await repo.create(DIALOG_ID, "own")
    other = await repo.create(OTHER_DIALOG_ID, "keep")

    await repo.delete_for_dialog(DIALOG_ID)

    with pytest.raises(ExchangeNotFoundError):
        await repo.get(own.id)
    assert (await repo.get(other.id)).id == other.id


async def test_exchange_set_title_renames_the_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    created = await repo.create(DIALOG_ID, EXCHANGE_TITLE)

    await repo.set_title(created.id, "what it is about now")

    assert (await repo.get(created.id)).title == "what it is about now"


async def test_exchange_set_title_clamps_to_the_stored_length(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The title is a label — the console, the nudge and the router's
    candidate lines all render it, and the full text lives in the message."""
    repo = SqlAlchemyExchangeRepository(session_factory)
    created = await repo.create(DIALOG_ID, EXCHANGE_TITLE)

    await repo.set_title(created.id, "y" * (TITLE_MAX_LENGTH * 3))

    assert (await repo.get(created.id)).title == "y" * TITLE_MAX_LENGTH


async def test_exchange_set_title_on_a_missing_row_is_a_noop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Renaming is cosmetic: an exchange deleted under it must not raise the
    way `set_status` does, or a name would cost the answer."""
    repo = SqlAlchemyExchangeRepository(session_factory)

    await repo.set_title("no-such-exchange", "a name")


async def test_exchange_create_with_collecting_status_starts_collecting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A `status=` override wins over the owner-based default: COLLECTING must
    never pass through OPEN-and-unowned, not even briefly (the unowned-open
    sweep would grab it as work to do)."""
    repo = SqlAlchemyExchangeRepository(session_factory)

    exchange = await repo.create(DIALOG_ID, COLLECTING_TITLE, status=ExchangeStatus.COLLECTING)

    assert exchange.status is ExchangeStatus.COLLECTING
    assert exchange.title == COLLECTING_TITLE


async def test_find_collecting_returns_the_dialogs_collecting_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    collecting = await repo.create(DIALOG_ID, COLLECTING_TITLE, status=ExchangeStatus.COLLECTING)
    await repo.create(DIALOG_ID, "an open question")  # live, but not a collection

    found = await repo.find_collecting(DIALOG_ID)

    assert found is not None
    assert found.id == collecting.id


async def test_find_collecting_returns_none_without_a_collection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    await repo.create(DIALOG_ID, "an open question")

    assert await repo.find_collecting(DIALOG_ID) is None


async def test_find_collecting_returns_none_once_promoted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    collecting = await repo.create(DIALOG_ID, COLLECTING_TITLE, status=ExchangeStatus.COLLECTING)

    await repo.set_status(collecting.id, ExchangeStatus.OPEN)

    assert await repo.find_collecting(DIALOG_ID) is None


async def test_list_stale_collecting_only_returns_exchanges_past_the_quiet_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    now = utc_now()
    stale_id = await _insert_exchange(
        session_factory,
        title="stale",
        status=ExchangeStatus.COLLECTING,
        created_at=now - timedelta(seconds=STALE_QUIET_SECONDS + 60),
        updated_at=now - timedelta(seconds=STALE_QUIET_SECONDS + 1),
    )
    await _insert_exchange(  # touched too recently: must not come back yet
        session_factory,
        title="fresh",
        status=ExchangeStatus.COLLECTING,
        created_at=now - timedelta(seconds=STALE_QUIET_SECONDS + 60),
        updated_at=now,
    )
    await _insert_exchange(  # not a collection at all, however stale
        session_factory,
        title="open",
        status=ExchangeStatus.OPEN,
        created_at=now - timedelta(seconds=STALE_QUIET_SECONDS + 60),
        updated_at=now - timedelta(seconds=STALE_QUIET_SECONDS + 60),
    )

    stale = await repo.list_stale_collecting(STALE_QUIET_SECONDS)

    assert [item.id for item in stale] == [stale_id]


async def test_touch_moves_updated_at_forward(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemyExchangeRepository(session_factory)
    old_timestamp = CREATED_EARLIER
    exchange_id = await _insert_exchange(
        session_factory,
        title=COLLECTING_TITLE,
        status=ExchangeStatus.COLLECTING,
        created_at=old_timestamp,
        updated_at=old_timestamp,
    )

    await repo.touch(exchange_id)

    touched = await repo.get(exchange_id)
    assert touched.updated_at > old_timestamp


async def test_sql_stores_satisfy_the_dialogs_ports(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The SQL implementations must keep matching the api.py Protocols.

    The annotations make mypy verify the structural match; the calls keep the
    port surface exercised through the protocol type, so a signature drift
    fails both ways (audit item 5 — the actor now types against the ports).
    """
    dialogs: DialogRepository = SqlAlchemyDialogRepository(session_factory)
    messages: MessageRepository = SqlAlchemyMessageRepository(session_factory)

    dialog = await dialogs.get_or_create("port-user", "web")
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="hi"))

    assert [m.content for m in await messages.list(dialog.id)] == ["hi"]
    assert await messages.list_after(dialog.id, 1) == []
    assert (await dialogs.get(dialog.id)).id == dialog.id


async def test_message_kind_round_trips_and_legacy_rows_are_own(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the exceptional kind is stored; a NULL column means the user's own words."""
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="мой вопрос"))
    await messages.append(
        dialog.id,
        ChatMessage(role=MessageRole.USER, content="чужой текст", kind=MessageKind.MATERIAL),
    )

    own, material = await messages.list(dialog.id)

    assert own.kind is MessageKind.OWN
    assert material.kind is MessageKind.MATERIAL
    async with session_factory() as session:
        rows = (await session.scalars(select(MessageRow).order_by(MessageRow.seq))).all()
    assert [row.kind for row in rows] == [None, MessageKind.MATERIAL.value]
