"""Behavioral contract of the unit of work: one transaction over several store calls.

The stores keep their ports; the unit travels in a ContextVar. What must hold:
writes inside a unit land together or not at all (across different stores), a
unit refuses to nest, concurrent store calls inside a unit fail loudly with a
message naming `outside_uow`, and `outside_uow` really does give a store call
a session of its own while a unit is active.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.unit_of_work import UnitOfWork, outside_uow, read_session
from octoforge_core.dialogs.api import (
    Exchange,
    ExchangeNotFoundError,
    ExchangeStatus,
    MessageAppend,
)
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.tasks.api import Task, TaskKind, TaskNotFoundError
from octoforge_core.tasks.store import SqlAlchemyTaskStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def uow(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWork:
    return UnitOfWork(session_factory)


@pytest.fixture
def exchanges(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyExchangeRepository:
    return SqlAlchemyExchangeRepository(session_factory)


@pytest.fixture
def tasks(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemyTaskStore:
    return SqlAlchemyTaskStore(session_factory)


@dataclass(frozen=True, slots=True)
class DatabaseFixtures:
    sessions: async_sessionmaker[AsyncSession]
    exchanges: SqlAlchemyExchangeRepository
    tasks: SqlAlchemyTaskStore


@pytest.fixture
def database(
    session_factory: async_sessionmaker[AsyncSession],
    exchanges: SqlAlchemyExchangeRepository,
    tasks: SqlAlchemyTaskStore,
) -> DatabaseFixtures:
    return DatabaseFixtures(session_factory, exchanges, tasks)


@pytest.fixture
async def dialog_id(session_factory: async_sessionmaker[AsyncSession]) -> str:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create("user-a", "web")
    return dialog.id


async def test_writes_of_one_unit_commit_together(
    uow: UnitOfWork,
    database: DatabaseFixtures,
    dialog_id: str,
) -> None:
    """Calls on different stores inside one unit all land at exit."""
    async with uow():
        exchange = await database.exchanges.create(dialog_id, "question")
        task = Task(dialog_id=dialog_id, title="answer", kind=TaskKind.ANSWER, input={})
        await database.tasks.add(task)
        await database.exchanges.set_status(exchange.id, ExchangeStatus.IN_PROGRESS)
    settled = await database.exchanges.get(exchange.id)
    assert settled.status is ExchangeStatus.IN_PROGRESS
    assert (await database.tasks.get(task.id)).id == task.id


async def test_an_error_rolls_the_whole_unit_back(
    uow: UnitOfWork,
    database: DatabaseFixtures,
    dialog_id: str,
) -> None:
    """No partial state survives: every write of the failed unit is gone."""
    task = Task(dialog_id=dialog_id, title="answer", kind=TaskKind.ANSWER, input={})
    exchange_id = ""
    with pytest.raises(RuntimeError, match="boom"):
        async with uow():
            exchange = await database.exchanges.create(dialog_id, "question")
            exchange_id = exchange.id
            await database.tasks.add(task)
            raise RuntimeError("boom")
    with pytest.raises(ExchangeNotFoundError):
        await database.exchanges.get(exchange_id)
    with pytest.raises(TaskNotFoundError):
        await database.tasks.get(task.id)


async def test_a_unit_refuses_to_nest(uow: UnitOfWork) -> None:
    """Two merged transactions believing they are independent is always a bug."""
    async with uow():
        with pytest.raises(RuntimeError, match="already active"):
            async with uow():
                pytest.fail("the nested unit must not open")


async def test_concurrent_store_calls_inside_a_unit_are_refused(
    uow: UnitOfWork,
    exchanges: SqlAlchemyExchangeRepository,
    dialog_id: str,
) -> None:
    """A stray gather inside a unit dies loudly, naming the escape hatch."""
    async with uow():
        exchange = await exchanges.create(dialog_id, "question")
        in_flight = asyncio.create_task(exchanges.get(exchange.id))
        await asyncio.sleep(0)  # let it mark the unit's session busy and suspend on IO
        with pytest.raises(RuntimeError, match="outside_uow"):
            await exchanges.get(exchange.id)
        assert (await in_flight).id == exchange.id


async def test_outside_uow_runs_on_its_own_session(
    uow: UnitOfWork,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The wrapped call escapes the unit instead of sharing its session."""

    async def current_session() -> AsyncSession:
        async with read_session(session_factory) as session:
            return session

    async with uow():
        inside = await current_session()
        escaped = await outside_uow(current_session())
        assert inside is not escaped
        assert inside is await current_session()  # the unit's session, both times


async def test_without_a_unit_every_call_still_commits_itself(
    exchanges: SqlAlchemyExchangeRepository,
    dialog_id: str,
) -> None:
    """The pre-unit behavior is untouched: no unit, one transaction per call."""
    exchange = await exchanges.create(dialog_id, "question")
    assert (await exchanges.get(exchange.id)).status is ExchangeStatus.OPEN


async def test_the_null_unit_groups_nothing(
    exchanges: SqlAlchemyExchangeRepository,
    dialog_id: str,
) -> None:
    """`UnitOfWork(None)` is for stores with no shared SQL database: the block
    runs, and every call inside keeps committing itself."""
    null_unit = UnitOfWork(None)
    exchange_id = ""
    with pytest.raises(RuntimeError, match="boom"):
        async with null_unit():
            exchange = await exchanges.create(dialog_id, "question")
            exchange_id = exchange.id
            raise RuntimeError("boom")
    # no transaction to roll back: the write stands
    assert (await exchanges.get(exchange_id)).status is ExchangeStatus.OPEN


async def test_a_task_spawned_inside_a_unit_never_reuses_its_session(
    uow: UnitOfWork,
    exchanges: SqlAlchemyExchangeRepository,
    dialog_id: str,
) -> None:
    """A background task inherits the ContextVar but may outlive the unit; the
    stale reference must read as "no unit", not as a closed session."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def background() -> Exchange:
        started.set()
        await release.wait()  # deliberately outlive the unit
        return await exchanges.create(dialog_id, "from the background")

    async with uow():
        job = asyncio.create_task(background())
        await started.wait()
        await exchanges.create(dialog_id, "inside the unit")
    release.set()
    created = await job
    assert (await exchanges.get(created.id)).title == "from the background"


async def test_appends_join_the_unit(
    uow: UnitOfWork,
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
) -> None:
    """`append` writes through the unit's session: a rolled-back unit takes
    the message with it."""
    messages = SqlAlchemyMessageRepository(session_factory)
    with pytest.raises(RuntimeError, match="boom"):
        async with uow():
            await messages.append(
                MessageAppend(dialog_id, ChatMessage(role=MessageRole.USER, content="hi"))
            )
            raise RuntimeError("boom")
    assert await messages.list(dialog_id) == []


async def test_a_failed_append_attempt_spares_the_units_earlier_writes(
    uow: UnitOfWork,
    database: DatabaseFixtures,
    dialog_id: str,
) -> None:
    """The retry SAVEPOINT rolls back the attempt alone: after the violation
    the unit is intact and usable, and commits what came before."""
    messages = SqlAlchemyMessageRepository(database.sessions)
    original = ChatMessage(role=MessageRole.USER, content="hi")
    await messages.append(MessageAppend(dialog_id, original, client_message_id="dup"))
    async with uow():
        exchange = await database.exchanges.create(dialog_id, "question")
        with pytest.raises(IntegrityError):  # the idempotency key is taken
            await messages.append(MessageAppend(dialog_id, original, client_message_id="dup"))
        await database.exchanges.set_title(exchange.id, "still alive")
    assert (await database.exchanges.get(exchange.id)).title == "still alive"
    assert len(await messages.list(dialog_id)) == 1  # the duplicate never landed
