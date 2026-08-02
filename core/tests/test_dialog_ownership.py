"""What a process does when it is not the only one: recovery scope and stand-down.

Two failures are being prevented here, and neither of them raises anything on
its own:

- a starting instance reopening exchanges its peers are answering right now,
  which corrupts a live conversation while looking like ordinary recovery;
- two actors on one dialog after a handover, which the user sees as the same
  question answered twice.

The single-process case is asserted too. It is the one that runs in almost
every installation, and none of this may change it.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.events import Finished
from octoforge_core.agent.events import TextDelta as LoopTextDelta
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import ExchangeInfo, RouteDecision
from octoforge_core.agent.runner import (
    STREAM_CLOSED,
    ConversationManager,
    ConversationRunner,
    ManagerStores,
    OwnershipConfig,
    RunnerConfig,
)
from octoforge_core.composition import build_agent_loop
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.dialogs.models import DialogClaimRow
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.llm.events import StreamEvent
from octoforge_core.llm.events import StreamFinished as LlmStreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.ports import ToolSpec
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import SqlAlchemyTaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.registry import ToolRegistry

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_ID = "user-1"
OTHER_USER_ID = "user-2"
CHANNEL = "web"
NODE = "node-a"
PEER_NODE = "node-b"
SYSTEM_PROMPT = "you are a test"
REPLY = "done"
MAX_ITERATIONS = 3
MAX_PROCESSES = 4
STALE_AFTER_SECONDS = 30.0
# far enough past the staleness window that a peer's claim reads as abandoned
LONG_SILENCE = timedelta(seconds=STALE_AFTER_SECONDS * 4)
TIMEOUT_SECONDS = 5.0
POLL_SECONDS = 0.01


class ScriptedLLM:
    """LLMClient stub answering every stream with the same final reply."""

    async def complete(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self, messages: list[ChatMessage], tools: list[ToolSpec] | None = None
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=REPLY)
        yield LlmStreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content=REPLY))


class PassThroughRouter:
    """MessageRouter stub never starting background processes."""

    async def route(
        self, exchanges: tuple[ExchangeInfo, ...], message: str, max_exchanges: int
    ) -> RouteDecision:
        return RouteDecision()


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def make_manager(
    session_factory: async_sessionmaker[AsyncSession],
    node_id: str = NODE,
    tasks: SqlAlchemyTaskStore | None = None,
) -> ConversationManager:
    return ConversationManager(
        config=RunnerConfig(
            loop=build_agent_loop(ScriptedLLM(), ToolRegistry(), max_iterations=MAX_ITERATIONS),
            prompts=StaticPromptProvider({SYSTEM_PROMPT_NAME: SYSTEM_PROMPT}),
            router=PassThroughRouter(),
            max_processes=MAX_PROCESSES,
            compactor=NoopContextCompactor(),
        ),
        stores=ManagerStores(
            dialogs=SqlAlchemyDialogRepository(session_factory),
            messages=SqlAlchemyMessageRepository(session_factory),
            tasks=tasks if tasks is not None else SqlAlchemyTaskStore(session_factory),
            exchanges=SqlAlchemyExchangeRepository(session_factory),
            claims=SqlAlchemyClaimRepository(session_factory),
            uow=UnitOfWork(session_factory),
        ),
        ownership=OwnershipConfig(
            node_id=node_id, heartbeat_seconds=0.01, stale_after_seconds=STALE_AFTER_SECONDS
        ),
    )


async def make_dialog(
    session_factory: async_sessionmaker[AsyncSession], user_id: str = USER_ID
) -> str:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(user_id, CHANNEL)
    return dialog.id


async def age_claim(
    session_factory: async_sessionmaker[AsyncSession], dialog_id: str, by: timedelta
) -> None:
    """Backdate a claim's heartbeat, standing in for a process that went quiet."""
    async with session_factory() as session:
        await session.execute(
            update(DialogClaimRow)
            .where(DialogClaimRow.dialog_id == dialog_id)
            .values(heartbeat_at=utc_now() - by)
        )
        await session.commit()


# --------------------------------------------------------------------------
# recovery scope
# --------------------------------------------------------------------------


async def test_recovery_leaves_alone_a_dialog_a_live_peer_is_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The failure this prevents: a rolling deploy resets the exchange the
    other instance is answering, and the user's question dies mid-answer."""
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    dialog_id = await make_dialog(session_factory)
    running = await exchanges.create(
        dialog_id, "peer is answering this", status=ExchangeStatus.IN_PROGRESS
    )
    await SqlAlchemyClaimRepository(session_factory).claim(dialog_id, PEER_NODE)

    await make_manager(session_factory).recover_interrupted()

    untouched = await exchanges.get(running.id)
    assert untouched.status is ExchangeStatus.IN_PROGRESS


async def test_recovery_takes_a_dialog_whose_owner_went_quiet(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    dialog_id = await make_dialog(session_factory)
    stranded = await exchanges.create(
        dialog_id, "its owner died", status=ExchangeStatus.IN_PROGRESS
    )
    await SqlAlchemyClaimRepository(session_factory).claim(dialog_id, PEER_NODE)
    await age_claim(session_factory, dialog_id, LONG_SILENCE)

    manager = make_manager(session_factory)
    try:
        await manager.recover_interrupted()
    finally:
        await manager.stop_all()

    reopened = await exchanges.get(stranded.id)
    assert reopened.status is ExchangeStatus.OPEN


async def test_a_restart_recovers_its_own_work_without_waiting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ordinary case: this instance restarted. Its own claim cannot be
    running — the process that made it is the one now asking — so making it
    wait out the staleness window would stall every restart."""
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    dialog_id = await make_dialog(session_factory)
    stranded = await exchanges.create(
        dialog_id, "mine from before", status=ExchangeStatus.IN_PROGRESS
    )
    await SqlAlchemyClaimRepository(session_factory).claim(dialog_id, NODE)

    manager = make_manager(session_factory, node_id=NODE)
    try:
        await manager.recover_interrupted()
    finally:
        await manager.stop_all()

    assert (await exchanges.get(stranded.id)).status is ExchangeStatus.OPEN


async def test_work_stranded_before_claims_existed_is_still_recovered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Candidates come from the work, not from the claim table: an upgrade
    must not orphan whatever was already stranded in the database."""
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    dialog_id = await make_dialog(session_factory)
    stranded = await exchanges.create(dialog_id, "no claim ever", status=ExchangeStatus.IN_PROGRESS)

    manager = make_manager(session_factory)
    try:
        await manager.recover_interrupted()
    finally:
        await manager.stop_all()

    assert (await exchanges.get(stranded.id)).status is ExchangeStatus.OPEN


async def test_recovery_splits_by_dialog_not_by_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One instance's stranded work and a peer's live work in one sweep: only
    the first moves."""
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    mine_id = await make_dialog(session_factory, USER_ID)
    theirs_id = await make_dialog(session_factory, OTHER_USER_ID)
    mine = await exchanges.create(mine_id, "mine", status=ExchangeStatus.IN_PROGRESS)
    theirs = await exchanges.create(theirs_id, "theirs", status=ExchangeStatus.IN_PROGRESS)
    await SqlAlchemyClaimRepository(session_factory).claim(theirs_id, PEER_NODE)

    manager = make_manager(session_factory)
    try:
        await manager.recover_interrupted()
    finally:
        await manager.stop_all()

    assert (await exchanges.get(mine.id)).status is ExchangeStatus.OPEN
    assert (await exchanges.get(theirs.id)).status is ExchangeStatus.IN_PROGRESS


async def test_an_orphaned_task_of_a_peers_dialog_is_not_restarted_here(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Restarting it would run the peer's background work a second time."""
    tasks = SqlAlchemyTaskStore(session_factory)
    dialog_id = await make_dialog(session_factory)
    await tasks.add(
        Task(
            id="task-1",
            dialog_id=dialog_id,
            kind=TaskKind.RUN,
            title="peer's background work",
            input={},
            status=TaskStatus.RUNNING,
        )
    )
    claims = SqlAlchemyClaimRepository(session_factory)
    held = await claims.claim(dialog_id, PEER_NODE)

    manager = make_manager(session_factory, tasks=tasks)
    try:
        await manager.recover_interrupted()
    finally:
        await manager.stop_all()

    # restarting the task would mean building its actor here, and building one
    # means claiming the dialog — so an untouched generation is the proof
    assert await claims.current_generation(dialog_id) == held.generation
    assert (await tasks.get("task-1")).status is TaskStatus.RUNNING


# --------------------------------------------------------------------------
# stand-down
# --------------------------------------------------------------------------


async def test_a_preempted_runner_stands_down_and_ends_its_streams(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """What the user must not get: a connection to a process that has stopped
    speaking for them. The stream ends, and reconnecting finds the new owner.
    """
    manager = make_manager(session_factory)
    try:
        runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
        queue = runner.subscribe()

        await SqlAlchemyClaimRepository(session_factory).claim(runner.dialog_id, PEER_NODE)
        await manager._beat_once()

        assert queue.get_nowait() is STREAM_CLOSED
    finally:
        await manager.stop_all()


async def test_the_next_contact_after_a_stand_down_gets_a_fresh_runner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(session_factory)
    try:
        first = await manager.get_or_create_runner(USER_ID, CHANNEL)
        await SqlAlchemyClaimRepository(session_factory).claim(first.dialog_id, PEER_NODE)
        await manager._beat_once()

        second = await manager.get_or_create_runner(USER_ID, CHANNEL)

        assert second is not first
        assert second.claim.generation > first.claim.generation
    finally:
        await manager.stop_all()


async def test_subscribing_to_a_runner_that_already_left_says_so_at_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Silence would look exactly like the agent ignoring the user."""
    manager = make_manager(session_factory)
    try:
        runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
        await SqlAlchemyClaimRepository(session_factory).claim(runner.dialog_id, PEER_NODE)
        await manager._beat_once()

        assert runner.subscribe().get_nowait() is STREAM_CLOSED
    finally:
        await manager.stop_all()


async def test_a_run_refuses_to_start_once_the_dialog_has_moved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The per-run check: without it, a message already in flight would be
    answered by a process that no longer owns the dialog — and the new owner
    would answer it too."""
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    manager = make_manager(session_factory)
    try:
        runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
        # taken away without the heartbeat having noticed yet
        await SqlAlchemyClaimRepository(session_factory).claim(runner.dialog_id, PEER_NODE)

        await runner.submit("what is the budget?")
        # the obligation is recorded, then the run is refused: what is left
        # behind must be work the new owner can pick up
        await _wait_for(lambda: _only_open(exchanges, runner.dialog_id))

        live = await exchanges.list_live(runner.dialog_id)
        assert [exchange.status for exchange in live] == [ExchangeStatus.OPEN]
    finally:
        await manager.stop_all()


async def test_one_process_answers_exactly_as_it_always_did(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The installation that will never have a second process must not pay
    for any of this."""
    manager = make_manager(session_factory)
    try:
        runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
        queue = runner.subscribe()
        await runner.submit("what is the budget?")

        seen = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=TIMEOUT_SECONDS)
            assert event is not STREAM_CLOSED
            seen.append(event)
            if isinstance(event.payload, Finished):
                break
        assert any(isinstance(event.payload, LoopTextDelta) for event in seen)
    finally:
        await manager.stop_all()


async def _only_open(exchanges: SqlAlchemyExchangeRepository, dialog_id: str) -> bool:
    live = await exchanges.list_live(dialog_id)
    return bool(live) and all(exchange.status is ExchangeStatus.OPEN for exchange in live)


async def _wait_for(predicate: Callable[[], Awaitable[bool]]) -> None:
    async def _poll() -> None:
        while not await predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(_poll(), timeout=TIMEOUT_SECONDS)


async def test_taking_a_dialog_over_recovers_its_stranded_answer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A handover strands whatever the previous owner was answering, and the
    startup sweep will never come back for it: this process now holds a fresh
    claim, so every peer's recovery skips the dialog. Claiming has to recover
    what it claims, or a mid-answer deploy loses the answer for good.
    """
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    dialog_id = await make_dialog(session_factory)
    stranded = await exchanges.create(
        dialog_id, "answered by the old owner", status=ExchangeStatus.IN_PROGRESS
    )
    await SqlAlchemyClaimRepository(session_factory).claim(dialog_id, PEER_NODE)

    manager = make_manager(session_factory)
    try:
        await manager.get_or_create_runner(USER_ID, CHANNEL)
        await _wait_for(lambda: _left_in_progress(exchanges, stranded.id))
    finally:
        await manager.stop_all()

    assert (await exchanges.get(stranded.id)).status is not ExchangeStatus.IN_PROGRESS


async def _left_in_progress(exchanges: SqlAlchemyExchangeRepository, exchange_id: str) -> bool:
    return (await exchanges.get(exchange_id)).status is not ExchangeStatus.IN_PROGRESS


class RecordingSurface:
    """DialogSurface stub recording which dialogs it was asked to render."""

    def __init__(self) -> None:
        self.attached: list[str] = []
        self.detached: list[str] = []

    async def attach(self, runner: object) -> None:
        self.attached.append(runner.dialog_id)

    async def detach(self, runner: object) -> None:
        self.detached.append(runner.dialog_id)


class FailingSurface:
    """DialogSurface stub that cannot attach."""

    async def attach(self, runner: object) -> None:
        raise RuntimeError("transport down")

    async def detach(self, runner: object) -> None:
        raise RuntimeError("transport down")


async def test_a_dialog_gets_its_surface_when_its_actor_is_built(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rendering is tied to the actor, not to a request: a scheduled run
    finishing while nobody is looking still has to reach the user."""
    surface = RecordingSurface()
    manager = make_manager(session_factory)
    manager.use_surface(surface)
    try:
        runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
        assert surface.attached == [runner.dialog_id]
    finally:
        await manager.stop_all()

    assert surface.detached == [runner.dialog_id]


async def test_a_dialog_recovered_at_startup_gets_its_surface(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Recovery is the only thing that reaches a dialog nobody has written to yet.

    A cron answer that finished while this process was down is redelivered by
    recovery, into a dialog whose actor recovery itself builds. If the
    transport is registered after that, the answer is redelivered to nobody
    and waits — still marked undelivered — until its user happens to write.
    Nothing prepares dialogs at startup any more, so this attach is what
    delivers it.
    """
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    dialog_id = await make_dialog(session_factory)
    await exchanges.create(dialog_id, "stranded by the restart", status=ExchangeStatus.IN_PROGRESS)

    surface = RecordingSurface()
    manager = make_manager(session_factory)
    manager.use_surface(surface)
    try:
        await manager.recover_interrupted()

        assert surface.attached == [dialog_id]
    finally:
        await manager.stop_all()


async def test_a_dialog_that_moved_away_loses_its_surface(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two processes rendering one chat would answer the user twice."""
    surface = RecordingSurface()
    manager = make_manager(session_factory)
    manager.use_surface(surface)
    try:
        runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
        await SqlAlchemyClaimRepository(session_factory).claim(runner.dialog_id, PEER_NODE)
        await manager._beat_once()

        assert surface.detached == [runner.dialog_id]
    finally:
        await manager.stop_all()


async def test_a_surface_that_cannot_attach_does_not_break_the_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A transport that is down costs delivery through that transport; the
    dialog itself, and the API subscription path, keep working."""
    manager = make_manager(session_factory)
    manager.use_surface(FailingSurface())
    try:
        runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
        assert runner.dialog_id
    finally:
        await manager.stop_all()


class ResolvingSurface:
    """A surface that looks its runner up the way a chat bridge does."""

    def __init__(self, manager: ConversationManager) -> None:
        self._manager = manager
        self.attached = 0

    async def attach(self, runner: ConversationRunner) -> None:
        await self._manager.get_or_create_runner(runner.user_id, runner.channel)
        self.attached += 1

    async def detach(self, runner: ConversationRunner) -> None:
        pass


async def test_a_surface_may_resolve_the_runner_it_is_being_given(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Attaching used to happen inside the build, so a surface that resolved
    runners through this manager made the build await the task it was running
    in — the dialog hung on first contact, forever."""
    manager = make_manager(session_factory)
    surface = ResolvingSurface(manager)
    manager.use_surface(surface)
    try:
        runner = await asyncio.wait_for(
            manager.get_or_create_runner(USER_ID, CHANNEL), timeout=TIMEOUT_SECONDS
        )
        assert runner.dialog_id
        assert surface.attached == 1
    finally:
        await manager.stop_all()


async def test_concurrent_first_contacts_attach_the_surface_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the caller that started the build attaches; the others share it."""
    surface = RecordingSurface()
    manager = make_manager(session_factory)
    manager.use_surface(surface)
    try:
        await asyncio.gather(*(manager.get_or_create_runner(USER_ID, CHANNEL) for _ in range(4)))
        assert len(surface.attached) == 1
    finally:
        await manager.stop_all()
