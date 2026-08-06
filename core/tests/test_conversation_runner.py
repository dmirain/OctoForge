"""Tests for ConversationRunner processes and ConversationManager."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.branch import (
    CLARIFICATION_NOTE_TEMPLATE,
    MATERIAL_NOTE_TEMPLATE,
    MATERIAL_TASK_NOTE_TEMPLATE,
    TASK_NOTE_TEMPLATE,
)
from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    LoopEvent,
    ProcessCompleted,
    ProcessStarted,
    TextDelta,
    ToolCallRequested,
)
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import (
    ExchangeInfo,
    MessageRouter,
    RouteAction,
    RouteDecision,
)
from octoforge_core.agent.runner import (
    BACKGROUND_TASK_PROMPT,
    CLAIM_STALE_AFTER_SECONDS,
    MATERIAL_DIGEST_CHARS,
    MATERIAL_QUIET_SECONDS,
    MATERIAL_TITLE_ANONYMOUS,
    MATERIAL_TITLE_TEMPLATE,
    NUDGE_AFTER_SECONDS,
    NUDGE_TEMPLATE,
    RESTART_LIMIT_ERROR,
    SUBMIT_FAILED_ERROR,
    SUBSCRIBER_QUEUE_SIZE,
    ConversationEvent,
    ConversationManager,
    ConversationRunner,
    ManagerStores,
    OwnershipConfig,
    RunnerConfig,
    TaskOutcomeListener,
    _Flush,
    _ProcessTerminated,
    _Unseen,
)
from octoforge_core.context.api import (
    INTERRUPTED_NOTE,
    AssembledContext,
    ContextCompactor,
    DialogueSummary,
)
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.cron.api import CronWaker, WakeOutcome
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.dialogs.models import ExchangeRow, MessageRow
from octoforge_core.dialogs.store import (
    SqlAlchemyClaimRepository,
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import (
    Attachment,
    AttachmentKind,
    ChatMessage,
    Dialog,
    MessageKind,
    MessageRole,
    MessageSource,
    ToolCall,
)
from octoforge_core.llm.errors import ContextOverflowError
from octoforge_core.llm.events import StreamEvent, StreamFinished, ToolCallReady
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion, Usage
from octoforge_core.ports import LLMClient
from octoforge_core.tariffs.api import (
    LimitVerdict,
    UsageEvent,
    UsageKind,
    UsageOrigin,
)
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import InMemoryTaskStore, SqlAlchemyTaskStore, TaskStore
from octoforge_core.tasks.tools import TaskCreateTool, TaskDeleteTool
from octoforge_core.time import utc_now
from octoforge_core.tools.base import TaskDeleteOutcome, ToolContext, ToolSpec
from octoforge_core.tools.registry import ToolRegistry
from octoforge_core.vision.api import (
    ImageData,
    ImageResolver,
    VisionClient,
    VisionUnavailableError,
)

PROMPT = "test system prompt"
REPLY = "hello"
PARTIAL = "partial"
PROMPT_TOKENS = 321
COMPLETION_TOKENS = 12
RETRIED_CALLS = 2
FIRST_CLIENT_KEY = "upd-1"
SECOND_CLIENT_KEY = "upd-2"
SECOND_REPLY = "world"
BLOCKING_TOOL = "blocking"
TASK_CREATE_CALL = "task_create"
CALL_ID = "call-1"
TASK_RESULT = "42"
TASK_TITLE = "research"
TASK_PROMPT = "solve 2+2"
UNBLOCKED_OUTPUT = "unblocked"
PROVIDER_ERROR_MESSAGE = "provider down"
TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.01
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
ONE_PROCESS = 1
TWO_PROCESSES = 2
NODE_ID = "test-node"
USER_ID = "user-1"
CHANNEL = "web"
OLD_IMAGE = Attachment(kind=AttachmentKind.IMAGE, ref="tg:file-1")
NEW_IMAGE = Attachment(kind=AttachmentKind.IMAGE, ref="tg:file-42")
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
FIRST_CALL = 1
SECOND_CALL = 2
EXPECTED_LLM_CALLS = 3
MESSAGES_AFTER_REFUSAL = 3
MESSAGES_AFTER_INJECT = 2
OTHER_TASK_ID = "other"
DEAD_TASK_ID = "dead"
NUDGE_STALE_SECONDS = NUDGE_AFTER_SECONDS + 100.0
TWO_PENDING_DELIVERIES = 2
MATERIAL_ORIGIN = "Ivan Petrov"
MATERIAL_ONE = "forwarded one"
MATERIAL_TWO = "forwarded two"
MATERIAL_THREE = "forwarded three"
THREE_MATERIAL_MESSAGES = 3
TWO_MESSAGES = 2
FOUR_MESSAGES = 4
MATERIAL_REACTION = "reacted to the batch"
ADOPTING_TASK_ID = "adopting-task"
TARGET_QUESTION = "target question"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


class FakeRouter:
    """MessageRouter stub: NEW by default, CONTINUE into the first live exchange."""

    def __init__(self) -> None:
        self.action = RouteAction.NEW
        self.cancel_all = False
        self.cancel_one: str | None = None
        self.title: str | None = None
        self.usage: Usage | None = None
        self.calls: list[tuple[tuple[ExchangeInfo, ...], str]] = []

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        self.calls.append((exchanges, message))
        cancel_ids: tuple[str, ...] = ()
        if self.cancel_all:
            cancel_ids = tuple(item.id for item in exchanges)
        elif self.cancel_one is not None:
            cancel_ids = (self.cancel_one,)
        if self.action is RouteAction.CONTINUE and exchanges:
            return RouteDecision(
                action=RouteAction.CONTINUE,
                exchange_id=exchanges[0].id,
                cancel_ids=cancel_ids,
                title=self.title,
                usage=self.usage,
            )
        return RouteDecision(action=self.action, cancel_ids=cancel_ids, usage=self.usage)

    def decide_continue(self, title: str | None = None) -> None:
        """Route the next message into the oldest live exchange (the old INJECT)."""
        self.action = RouteAction.CONTINUE
        self.title = title

    def decide_cancel_all(self) -> None:
        self.cancel_all = True

    def decide_cancel(self, exchange_id: str) -> None:
        """Cancel one named exchange while the message opens its own."""
        self.cancel_one = exchange_id


class GatedRouter:
    """MessageRouter stub stalling inside route() until the test releases it.

    Models the real window: the router is an LLM call, so the actor sits in it
    while the foreground finishes and its final lands in the narrative.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.gate_armed = False

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        if self.gate_armed:
            self.entered.set()
            await self.release.wait()
        return RouteDecision()


class ScriptedLLM:
    """LLMClient stub replaying scripted replies as streams."""

    def __init__(self, replies: list[ChatMessage]) -> None:
        self._replies = list(replies)
        self.requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        self.requests.append(list(messages))
        return Completion(message=self._replies.pop(0))

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        reply = self._replies.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply)


class UsageLLM(ScriptedLLM):
    """ScriptedLLM attaching provider usage to the stream finish."""

    def __init__(self, replies: list[ChatMessage], usage: Usage) -> None:
        super().__init__(replies)
        self._usage = usage

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        reply = self._replies.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply, usage=self._usage)


class FakeCompactor:
    """ContextCompactor stub: passthrough assemble, programmable compact_now."""

    def __init__(self, compact_result: bool = True, compact_error: Exception | None = None) -> None:
        self._compact_result = compact_result
        self._compact_error = compact_error
        self.compact_calls = 0
        self.closed: list[str] = []

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        return AssembledContext(
            messages=list(history), tail_count=len(history), snapshot_len=len(history)
        )

    async def compacted_boundary(self, dialog_id: str) -> int:
        return 0

    async def compact_now(self, dialog: Dialog) -> bool:
        self.compact_calls += 1
        if self._compact_error is not None:
            raise self._compact_error
        return self._compact_result

    async def aclose(self, dialog_id: str) -> None:
        self.closed.append(dialog_id)


class RecordingLimitGate:
    """LimitGate stub: allows by default, refusals scripted, events collected."""

    def __init__(self) -> None:
        self.events: list[UsageEvent] = []
        self.run_verdict = LimitVerdict.ok()
        self.features: frozenset[str] | None = None

    async def enabled_features(self, user_id: str) -> frozenset[str] | None:
        return self.features

    async def allows(self, user_id: str, feature: str) -> bool:
        return self.features is None or feature in self.features

    async def check_run_budget(self, user_id: str) -> LimitVerdict:
        return self.run_verdict

    async def max_cron_jobs(self, user_id: str) -> int | None:
        return None

    async def max_datasets(self, user_id: str) -> int | None:
        return None

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)

    def by_kind(self, kind: UsageKind) -> list[UsageEvent]:
        return [event for event in self.events if event.kind is kind]


class OverflowLLM:
    """LLMClient stub failing streams with ContextOverflowError, then answering."""

    def __init__(self, overflows: int) -> None:
        self._overflows = overflows
        self.stream_calls = 0
        self.requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.stream_calls += 1
        self.requests.append(list(messages))
        if self.stream_calls <= self._overflows:
            raise ContextOverflowError("prompt too big")
        yield LlmTextDelta(text=REPLY)
        yield StreamFinished(message=ChatMessage(role=MessageRole.ASSISTANT, content=REPLY))


class GatedLLM:
    """LLMClient stub pausing its second reply until released."""

    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        call_number = len(self.requests)
        if call_number == FIRST_CALL:
            yield StreamFinished(message=blocking_call())
        elif call_number == SECOND_CALL:
            yield LlmTextDelta(text=PARTIAL)
            await self.release.wait()
            yield StreamFinished(message=reply("first final"))
        else:
            yield StreamFinished(message=reply("after"))


class BranchLLM:
    """LLMClient stub dispatching replies by the request's system prompt.

    Background-task calls wait for a release gate when enabled, so tests
    control when the background process finishes.
    """

    def __init__(self, main: list[ChatMessage], background: list[ChatMessage]) -> None:
        self._main = list(main)
        self._background = list(background)
        self.main_requests: list[list[ChatMessage]] = []
        self.background_requests: list[list[ChatMessage]] = []
        self.background_release = asyncio.Event()
        self.gate_background = False

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if messages and messages[0].content.startswith(BACKGROUND_TASK_PROMPT):
            self.background_requests.append(list(messages))
            if self.gate_background:
                await self.background_release.wait()
            reply = self._background.pop(0)
        else:
            self.main_requests.append(list(messages))
            reply = self._main.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply)


class FailingTaskLLM:
    """LLMClient stub failing background-task streams with a provider error."""

    def __init__(self) -> None:
        self.background_requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.background_requests.append(list(messages))
        raise RuntimeError(PROVIDER_ERROR_MESSAGE)
        yield  # the raise above is the point; the yield keeps the generator shape


class StallingLLM:
    """LLMClient stub emitting partial text and stalling until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=PARTIAL)
        await self.release.wait()
        yield StreamFinished(message=reply("full"))


class ToolStallingLLM:
    """LLMClient stub emitting partial text plus a tool call, then stalling."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=PARTIAL)
        yield ToolCallReady(call=ToolCall(id=CALL_ID, name=BLOCKING_TOOL, arguments={}))
        await self.release.wait()
        yield StreamFinished(message=reply("full"))


class BlockingTool:
    """Tool stub holding the run open until released."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=BLOCKING_TOOL, description="blocks", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self.started.set()
        await self.release.wait()
        return UNBLOCKED_OUTPUT


class TwoPhaseBlockingTool:
    """Tool stub blocking each call on its own gate (multi-iteration scenarios)."""

    def __init__(self, phases: int = 2) -> None:
        self.started = [asyncio.Event() for _ in range(phases)]
        self.release = [asyncio.Event() for _ in range(phases)]
        self._calls = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=BLOCKING_TOOL, description="blocks per call", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        phase = self._calls
        self._calls += 1
        self.started[phase].set()
        await self.release[phase].wait()
        return UNBLOCKED_OUTPUT


class ExhaustedFailsLLM(ScriptedLLM):
    """ScriptedLLM that fails with a provider error once the script runs dry."""

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not self._replies:
            self.requests.append(list(messages))
            raise RuntimeError(PROVIDER_ERROR_MESSAGE)
        async for event in super().stream(messages, tools):
            yield event


class QuickTool:
    """Tool stub completing immediately."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=BLOCKING_TOOL, description="quick", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return "ok"


def blocking_call() -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=CALL_ID, name=BLOCKING_TOOL, arguments={}),),
    )


def task_create_call() -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(
            ToolCall(
                id=CALL_ID,
                name=TASK_CREATE_CALL,
                arguments={"title": TASK_TITLE, "prompt": TASK_PROMPT},
            ),
        ),
    )


def reply(content: str = REPLY) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


@dataclass(frozen=True, slots=True)
class ManagerOptions:
    """Optional knobs for building a test ConversationManager."""

    router: MessageRouter | None = None
    store: TaskStore | None = None
    max_processes: int = MAX_PROCESSES
    listener: TaskOutcomeListener | None = None
    compactor: ContextCompactor | None = None
    vision: VisionClient | None = None
    image_resolver: ImageResolver | None = None
    limits: RecordingLimitGate | None = None
    #: How long a claim may go unrefreshed before its owner reads as dead.
    stale_after_seconds: float = CLAIM_STALE_AFTER_SECONDS


def make_manager(
    llm: LLMClient,
    registry: ToolRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    options: ManagerOptions | None = None,
) -> ConversationManager:
    resolved = options if options is not None else ManagerOptions()
    loop = AgentLoop(llm_client=llm, registry=registry, max_iterations=MAX_ITERATIONS)
    config = RunnerConfig(
        loop=loop,
        prompts=StaticPromptProvider({SYSTEM_PROMPT_NAME: PROMPT}),
        router=resolved.router if resolved.router is not None else FakeRouter(),
        max_processes=resolved.max_processes,
        compactor=(
            resolved.compactor if resolved.compactor is not None else NoopContextCompactor()
        ),
        task_outcome_listener=resolved.listener,
        vision=resolved.vision,
        image_resolver=resolved.image_resolver,
        limits=resolved.limits,
    )
    return ConversationManager(
        config=config,
        stores=ManagerStores(
            dialogs=SqlAlchemyDialogRepository(session_factory),
            messages=SqlAlchemyMessageRepository(session_factory),
            tasks=resolved.store
            if resolved.store is not None
            else SqlAlchemyTaskStore(session_factory),
            exchanges=SqlAlchemyExchangeRepository(session_factory),
            claims=SqlAlchemyClaimRepository(session_factory),
            uow=UnitOfWork(session_factory),
        ),
        ownership=OwnershipConfig(
            node_id=NODE_ID, stale_after_seconds=resolved.stale_after_seconds
        ),
    )


async def get_dialog(session_factory: async_sessionmaker[AsyncSession]) -> Dialog:
    return await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_ID, CHANNEL)


async def single_task(store: TaskStore, dialog_id: str) -> Task:
    """Return the only stored task of the dialog."""
    (task,) = await store.list(dialog_id)
    return task


def blocking_registry(tool: BlockingTool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def quick_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(QuickTool())
    return registry


def is_completed(event: ConversationEvent) -> bool:
    return isinstance(event.payload, ProcessCompleted)


def is_delivered(content: str) -> Callable[[ConversationEvent], bool]:
    """Predicate matching a Finished event with the given content (a delivery)."""

    def _matches(event: ConversationEvent) -> bool:
        return isinstance(event.payload, Finished) and event.payload.message.content == content

    return _matches


async def next_event(queue: asyncio.Queue[ConversationEvent]) -> ConversationEvent:
    return await asyncio.wait_for(queue.get(), timeout=TIMEOUT_SECONDS)


async def collect_until(
    queue: asyncio.Queue[ConversationEvent],
    predicate: Callable[[ConversationEvent], bool],
) -> list[ConversationEvent]:
    events: list[ConversationEvent] = []
    while True:
        event = await next_event(queue)
        events.append(event)
        if predicate(event):
            return events


async def collect_completions(
    queue: asyncio.Queue[ConversationEvent], count: int
) -> list[ConversationEvent]:
    events: list[ConversationEvent] = []
    completed = 0
    while completed < count:
        event = await next_event(queue)
        events.append(event)
        if is_completed(event):
            completed += 1
    return events


def completions(events: list[ConversationEvent]) -> list[ProcessCompleted]:
    return [event.payload for event in events if isinstance(event.payload, ProcessCompleted)]


async def wait_for_message(runner: ConversationRunner, content: str) -> None:
    """Wait until a message with this content is in the narrative.

    Not `len(history) == N`. Where the message being waited for spawns a run
    of its own, the narrative only passes THROUGH N — the answer follows
    within the same poll gap — and an equality that overshoots is never true
    again, so the wait burns its whole timeout and reads as a hang. That is
    the intermittent failure of this file that was blamed first on a loaded
    host and then on the connection pool; it is neither, and the peak
    concurrent checkout count is 1.
    """
    await wait_for_condition(lambda: any(item.content == content for item in runner.history()))


async def wait_for_task_state(
    store: TaskStore, task_id: str, predicate: Callable[[Task], bool]
) -> Task:
    """Poll the STORE for the task until the predicate holds; return the fresh row.

    The SQL store hands out fresh DTOs, not aliases: a Task held from before
    a write shows the old state forever. The in-memory store's aliasing used
    to hide that — these tests asserted on objects, believing they asserted
    on persisted state.
    """
    latest: list[Task] = []

    async def _ok() -> bool:
        row = await store.get(task_id)
        latest[:] = [row]
        return predicate(row)

    await wait_for_async_condition(_ok)
    return latest[0]


async def wait_for_condition(predicate: Callable[[], bool]) -> None:
    async def _wait() -> None:
        while not predicate():
            await asyncio.sleep(POLL_SECONDS)

    await asyncio.wait_for(_wait(), timeout=TIMEOUT_SECONDS)


def test_loop_control_has_no_inject_channel() -> None:
    control = LoopControl()
    assert not hasattr(control, "inject")
    assert not hasattr(control, "drain")
    assert control.is_cancelled is False


async def test_submit_streams_events_and_updates_narrative(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([reply()]), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_completed)

    payloads = [event.payload for event in events]
    assert any(isinstance(payload, TextDelta) for payload in payloads)
    finished = [payload for payload in payloads if isinstance(payload, Finished)]
    assert len(finished) == 1
    assert finished[0].message.content == REPLY
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [("hi", TaskStatus.DONE.value)]
    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs)
    # every process is task-backed: the answer task links the final message
    task = await single_task(store, runner.dialog_id)
    assert task.kind is TaskKind.ANSWER
    source_message = runner.history()[0]
    assert source_message.id is not None  # captured from the persist
    assert task.input["prompt"] == "hi"
    assert task.input["source_message_id"] == source_message.id
    assert task.input["exchange_id"] == source_message.exchange_id
    assert task.status is TaskStatus.DONE
    # the foreground stream was the delivery: the actor stamps it asynchronously
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert not runner._pending_deliveries  # nothing left in the outbox
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="hi"),
        replace(reply(), task_id=task.id),
    ]
    dialog = await get_dialog(session_factory)
    assert await SqlAlchemyMessageRepository(session_factory).list(dialog.id) == runner.history()


async def test_finished_usage_is_persisted_on_the_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    usage = Usage(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS)
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        UsageLLM([reply()], usage), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_completed)

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert finished[0].usage == usage
    task = await single_task(store, runner.dialog_id)
    dialog = await get_dialog(session_factory)
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(MessageRow).where(MessageRow.dialog_id == dialog.id).order_by(MessageRow.seq)
            )
        ).all()
    assistant = rows[-1]
    assert assistant.role == MessageRole.ASSISTANT.value
    assert assistant.prompt_tokens == PROMPT_TOKENS
    assert assistant.completion_tokens == COMPLETION_TOKENS
    assert assistant.task_id == task.id


async def test_submit_and_answer_are_ledgered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    usage = Usage(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS)
    gate = RecordingLimitGate()
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        UsageLLM([reply()], usage),
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store, limits=gate),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    await collect_until(queue, is_completed)

    (user_event,) = gate.by_kind(UsageKind.USER_MESSAGE)
    assert user_event.user_id == USER_ID
    assert user_event.quantity == 1
    assert user_event.dialog_id == runner.dialog_id
    (answer,) = gate.by_kind(UsageKind.LLM_ANSWER)
    task = await single_task(store, runner.dialog_id)
    assert answer.origin is UsageOrigin.INTERACTIVE
    assert (answer.prompt_tokens, answer.completion_tokens) == (PROMPT_TOKENS, COMPLETION_TOKENS)
    assert answer.quantity == 1  # one visible assistant message
    assert answer.task_id == task.id
    assert answer.exchange_id == task.exchange_id
    assert answer.dialog_id == runner.dialog_id


async def test_multi_iteration_run_ledgers_the_accumulated_tokens(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The terminal event alone carries only the last iteration's usage; the
    ledger must hold the sum of every iteration of the run."""
    usage = Usage(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS)
    gate = RecordingLimitGate()
    manager = make_manager(
        UsageLLM([blocking_call(), reply()], usage),
        quick_registry(),
        session_factory,
        ManagerOptions(limits=gate),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    await collect_until(queue, is_completed)

    (answer,) = gate.by_kind(UsageKind.LLM_ANSWER)
    assert answer.prompt_tokens == 2 * PROMPT_TOKENS
    assert answer.completion_tokens == 2 * COMPLETION_TOKENS
    assert answer.quantity == 1


async def test_routing_llm_usage_is_ledgered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A message routed past a live exchange pays for its routing call."""
    tool = BlockingTool()
    router = FakeRouter()
    router.usage = Usage(prompt_tokens=20, completion_tokens=5)
    gate = RecordingLimitGate()
    llm = ScriptedLLM([blocking_call(), reply("second final"), reply("first final")])
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router, limits=gate),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")  # a live exchange exists: the router is asked
    await collect_until(queue, is_delivered("second final"))
    tool.release.set()
    await collect_until(queue, is_delivered("first final"))

    (routing,) = gate.by_kind(UsageKind.LLM_ROUTING)
    assert routing.user_id == USER_ID
    assert (routing.prompt_tokens, routing.completion_tokens) == (20, 5)
    assert routing.origin is UsageOrigin.INTERACTIVE


async def test_branch_keeps_system_prompt_stable_and_envelopes_last_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM([reply()])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    await collect_until(queue, is_completed)

    branch = llm.requests[0]
    assert branch[0] == ChatMessage(role=MessageRole.SYSTEM, content=PROMPT)  # no volatile date
    assert branch[-1].role is MessageRole.USER
    assert branch[-1].content.startswith("[Current date and time: ")
    assert branch[-1].content.endswith("\nhi")
    # the envelope is branch-only: the narrative and the store keep the clean copy
    assert runner.history()[0] == ChatMessage(role=MessageRole.USER, content="hi")
    dialog = await get_dialog(session_factory)
    stored = await SqlAlchemyMessageRepository(session_factory).list(dialog.id)
    assert stored[0] == ChatMessage(role=MessageRole.USER, content="hi")


async def test_stop_honors_a_cancellation_the_store_swallowed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A cancel absorbed by a store call must still end the actor.

    SQLAlchemy's greenlet bridge swallows the `CancelledError` of a query in
    flight and lets the await return normally, so the dispatch finishes, the
    loop parks on the inbox again, and `stop()` waits on a task that never
    ends. That deadlock hung app shutdown and admin dialog deletion on Python
    3.11 — the version the container and CI run, while a 3.12 dev venv hides
    it. The swallow is simulated here rather than provoked through the store,
    so the test pins the actor loop's contract on every version.
    """
    manager = make_manager(ScriptedLLM([]), ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    entered = asyncio.Event()

    async def swallowing_flush() -> None:
        entered.set()
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(TIMEOUT_SECONDS * 10)

    runner._flush_deliveries = swallowing_flush  # type: ignore[method-assign]
    runner._inbox.put_nowait(_Flush())
    await asyncio.wait_for(entered.wait(), timeout=TIMEOUT_SECONDS)
    actor = runner._actor_task
    assert actor is not None

    await asyncio.wait_for(runner.stop(), timeout=TIMEOUT_SECONDS)

    # stop() returning is not enough: it suppresses CancelledError, so a
    # rescued-by-timeout stop looks successful. The actor itself must be done.
    assert actor.done()
    await manager.stop_all()


async def test_evict_stops_the_runner_and_the_next_contact_rebuilds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM([reply(), reply(SECOND_REPLY)])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    await runner.submit("hi")
    await collect_until(queue, is_completed)

    await manager.evict(USER_ID, CHANNEL)
    await manager.evict("nobody", CHANNEL)  # nothing live: a no-op

    fresh = await manager.get_or_create_runner(USER_ID, CHANNEL)
    assert fresh is not runner
    # the fresh runner rebuilt its narrative from the persisted rows
    assert [m.content for m in fresh.history()] == ["hi", REPLY]


async def test_duplicate_client_message_id_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router = FakeRouter()
    llm = ScriptedLLM([reply(), reply(SECOND_REPLY)])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(router=router))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi", client_message_id=FIRST_CLIENT_KEY)
    await collect_until(queue, is_completed)
    await runner.submit("hi", client_message_id=FIRST_CLIENT_KEY)  # delivery retry
    await runner.submit("again", client_message_id=SECOND_CLIENT_KEY)
    await collect_until(queue, is_completed)

    # neither message needs the router (the first has nothing to belong to, the
    # second arrives once the first exchange is answered); the duplicate never
    # got that far either
    assert router.calls == []
    assert [m.content for m in runner.history()] == ["hi", REPLY, "again", SECOND_REPLY]


async def test_context_overflow_compacts_and_retries_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = OverflowLLM(overflows=1)
    compactor = FakeCompactor()
    manager = make_manager(
        llm, ToolRegistry(), session_factory, ManagerOptions(compactor=compactor)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_completed)

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert len(finished) == 1
    assert finished[0].message.content == REPLY
    assert compactor.compact_calls == 1
    assert llm.stream_calls == RETRIED_CALLS
    # the retried run got a rebuilt branch: system head + narrative
    second_request = llm.requests[1]
    assert second_request[0].role is MessageRole.SYSTEM
    assert second_request[-1].content.endswith("\nhi")  # the date envelope wraps it
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [("hi", TaskStatus.DONE.value)]


async def test_second_context_overflow_fails_the_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = OverflowLLM(overflows=RETRIED_CALLS)
    compactor = FakeCompactor()
    manager = make_manager(
        llm, ToolRegistry(), session_factory, ManagerOptions(compactor=compactor)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_completed)

    failed = [e.payload for e in events if isinstance(e.payload, Failed)]
    assert len(failed) == 1
    assert "ContextOverflowError" in failed[0].error
    assert compactor.compact_calls == 1  # exactly one reactive compaction
    assert llm.stream_calls == RETRIED_CALLS  # initial run + one retry
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [("hi", TaskStatus.FAILED.value)]


async def test_context_overflow_without_compaction_fails_immediately(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = OverflowLLM(overflows=1)  # NoopContextCompactor: compact_now -> False
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_completed)

    failed = [e.payload for e in events if isinstance(e.payload, Failed)]
    assert len(failed) == 1
    assert llm.stream_calls == 1  # no retry without a compaction


async def test_compaction_crash_fails_the_run_and_releases_the_slot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = OverflowLLM(overflows=1)
    compactor = FakeCompactor(compact_error=RuntimeError("store down"))
    manager = make_manager(
        llm, ToolRegistry(), session_factory, ManagerOptions(compactor=compactor)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_completed)

    failed = [e.payload for e in events if isinstance(e.payload, Failed)]
    assert len(failed) == 1
    assert "RuntimeError" in failed[0].error
    assert llm.stream_calls == 1  # no retry without a rebuilt branch
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [("hi", TaskStatus.FAILED.value)]

    # the crashed pump must not leak the slot: a follow-up runs a new foreground process
    await runner.submit("again")
    events = await collect_until(queue, is_completed)
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert len(finished) == 1
    assert finished[0].message.content == REPLY


async def test_answers_stream_concurrently_each_into_its_own_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Phase 4: no foreground — every answer streams live, tagged with its
    exchange, and each question is answered by exactly one run."""
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), reply("second final"), reply("first final")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first", client_message_id="101")
    events = await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second", client_message_id="102")
    # the second answer streams to completion while the first is still blocked
    events += await collect_until(queue, is_delivered("second final"))
    second_finished = next(e for e in events if isinstance(e.payload, Finished))
    assert second_finished.payload.message.content == "second final"
    assert second_finished.exchange_id is not None  # tagged for its own draft

    tool.release.set()
    events += await collect_until(queue, is_delivered("first final"))

    # the two streams are tagged with different exchanges
    tagged = {
        e.exchange_id
        for e in events
        if isinstance(e.payload, Finished) and e.payload.message.content.endswith("final")
    }
    assert len(tagged) == TWO_PROCESSES
    started_keys = [
        e.payload.source_client_message_id for e in events if isinstance(e.payload, ProcessStarted)
    ]
    assert sorted(started_keys) == ["101", "102"]
    # the second run never saw the first question (someone else owes it)
    queued_branch = llm.requests[1]
    assert not any(m.role is MessageRole.USER and m.content == "first" for m in queued_branch)
    tasks = await store.list(runner.dialog_id)
    assert {task.title for task in tasks} == {"first", "second"}
    for item in tasks:
        await wait_for_task_state(
            store, item.id, lambda t: t.status is TaskStatus.DONE and t.delivered_at is not None
        )
    history = runner.history()
    assert [m.content for m in history] == ["first", "second", "second final", "first final"]


async def test_inject_routed_message_is_pulled_into_the_next_iteration(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    router.decide_continue()
    await runner.submit("extra context")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    assert len(llm.requests) == 1  # INJECT is a no-op: no new run, no push channel
    tool.release.set()
    await collect_until(queue, is_completed)

    # the running process re-synced its branch: the message arrived at iteration 2
    second_request = llm.requests[1]
    contents = [m.content for m in second_request]
    assert any(m.role is MessageRole.USER and "extra context" in m.content for m in second_request)
    assert next(i for i, c in enumerate(contents) if "start" in c) < next(
        i for i, content in enumerate(contents) if "extra context" in content
    )
    # the mid-run arrival carries the note (the model is told to fold it into
    # the answer); the run's own question carries its own marker
    wrapped = CLARIFICATION_NOTE_TEMPLATE.format(content="extra context")
    assert any(wrapped in m.content for m in second_request)
    assert any(TASK_NOTE_TEMPLATE.format(content="start") in m.content for m in second_request)
    # a message the process did sync is not re-routed at finalize: one process only
    assert len(llm.requests) == RETRIED_CALLS
    assert [m.content for m in runner.history()] == ["start", "extra context", "after"]


async def test_answer_events_carry_the_source_client_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reply threading: ProcessStarted precedes the stream and both it and the
    Finished terminal carry the submit's client_message_id."""
    llm = ScriptedLLM([reply()])
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi", client_message_id="777")
    events = await collect_until(queue, is_completed)

    started = [e.payload for e in events if isinstance(e.payload, ProcessStarted)]
    assert [item.source_client_message_id for item in started] == ["777"]
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.source_client_message_id for item in finished] == ["777"]
    # the key is persisted with the task: recovery redelivery keeps threading
    (task,) = await store.list(runner.dialog_id)
    assert task.input["source_client_message_id"] == "777"
    # ProcessStarted arrived before the first delta
    kinds = [type(e.payload) for e in events]
    assert kinds.index(ProcessStarted) < kinds.index(TextDelta)


async def test_empty_final_is_silence_not_an_empty_bubble(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A process may deliberately answer nothing (its question was taken over):
    the task completes and is stamped delivered, but no empty message enters
    the narrative and nothing is queued for delivery."""
    llm = ScriptedLLM([reply("")])
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    await collect_until(queue, is_completed)

    (task,) = await store.list(runner.dialog_id)
    assert task.status is TaskStatus.DONE
    # the delivered stamp lands when the actor drains _ProcessTerminated,
    # slightly after the ProcessCompleted broadcast
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert not runner._pending_deliveries
    assert [m.content for m in runner.history()] == ["hi"]


async def _only_first_is_live(exchanges: SqlAlchemyExchangeRepository, dialog_id: str) -> bool:
    live = await exchanges.list_live(dialog_id)
    return [item.title for item in live] == ["first"]


async def _is_answered(exchanges: SqlAlchemyExchangeRepository, exchange_id: str) -> bool:
    return (await exchanges.get(exchange_id)).status is ExchangeStatus.ANSWERED


async def _is_cancelled(exchanges: SqlAlchemyExchangeRepository, exchange_id: str) -> bool:
    return (await exchanges.get(exchange_id)).status is ExchangeStatus.CANCELLED


async def _is_failed(exchanges: SqlAlchemyExchangeRepository, exchange_id: str) -> bool:
    return (await exchanges.get(exchange_id)).status is ExchangeStatus.FAILED


async def _is_collecting(exchanges: SqlAlchemyExchangeRepository, exchange_id: str) -> bool:
    return (await exchanges.get(exchange_id)).status is ExchangeStatus.COLLECTING


async def _collection_exists(exchanges: SqlAlchemyExchangeRepository, dialog_id: str) -> bool:
    return await exchanges.find_collecting(dialog_id) is not None


async def _has_title(exchanges: SqlAlchemyExchangeRepository, exchange_id: str, title: str) -> bool:
    return (await exchanges.get(exchange_id)).title == title


async def _backdate_exchange(
    session_factory: async_sessionmaker[AsyncSession], exchange_id: str, seconds: float
) -> None:
    """Push an exchange's `updated_at` into the past (nudge staleness needs it)."""
    async with session_factory() as session:
        await session.execute(
            update(ExchangeRow)
            .where(ExchangeRow.id == exchange_id)
            .values(updated_at=utc_now() - timedelta(seconds=seconds))
        )
        await session.commit()


async def wait_for_async_condition(
    condition: Callable[[], Awaitable[bool]],
    timeout: float = TIMEOUT_SECONDS,
) -> None:
    """Poll an awaitable condition (store state settles after the event)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await condition():
            return
        await asyncio.sleep(POLL_SECONDS)
    raise TimeoutError("condition was not met in time")


class AskingTool:
    """Tool stub asking the user a question through the runner port."""

    def __init__(self, question: str = "which city?") -> None:
        self.question = question
        self.called = asyncio.Event()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=BLOCKING_TOOL, description="asks", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        assert context.user_prompter is not None
        await context.user_prompter.ask(self.question)
        self.called.set()
        return "asked"


async def test_asking_the_user_keeps_the_exchange_open_and_resumes_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The scenario that shaped the model: a run needs input, so it asks —
    the obligation stays open, the reply resumes it, and the answer closes it."""
    tool = AskingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), reply(""), reply("+21 in Moscow")])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))

    # the question reached the user and the obligation is NOT closed
    live = await exchanges.list_live(runner.dialog_id)
    assert [item.status for item in live] == [ExchangeStatus.AWAITING_USER]
    assert live[0].pending_question == tool.question
    await wait_for_condition(lambda: not runner._processes)  # the slot was freed

    # the reply continues that exchange and a fresh run answers it
    router.decide_continue()
    await runner.submit("Moscow")
    await collect_until(queue, is_delivered("+21 in Moscow"))

    await wait_for_async_condition(
        lambda: _is_answered(exchanges, live[0].id)
    )  # the same obligation, now closed by a real answer
    assert [m.content for m in runner.history()] == [
        "what is the weather?",
        tool.question,
        "Moscow",
        "+21 in Moscow",
    ]


async def test_a_run_that_asked_says_nothing_more(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A run that called `ask_user` is muted for the rest of its life.

    The tool's ack tells it to end without writing anything else, but a model
    with nothing left to say narrates the ack instead ("question delivered,
    waiting for your answer") — a second, pointless message right under the
    question. Muting also keeps the obligation honest: the filler used to
    count as an answer and close the exchange, so the user's reply opened a
    new one instead of continuing this.
    """
    filler = "Question delivered - waiting for your answer."
    tool = AskingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), reply(filler), reply("+21 in Moscow")])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    events = await collect_until(queue, is_delivered(tool.question))
    await wait_for_condition(lambda: not runner._processes)

    # the filler reached neither the user nor the narrative
    while not queue.empty():
        events.append(queue.get_nowait())
    assert not any(filler in _rendered(event) for event in events)
    assert [m.content for m in runner.history()] == ["what is the weather?", tool.question]

    # and the obligation still waits for the user, question intact
    live = await exchanges.list_live(runner.dialog_id)
    assert [item.status for item in live] == [ExchangeStatus.AWAITING_USER]
    assert live[0].pending_question == tool.question

    # so the reply continues that same exchange rather than opening a new one
    router.decide_continue()
    await runner.submit("Moscow")
    await collect_until(queue, is_delivered("+21 in Moscow"))
    await wait_for_async_condition(lambda: _is_answered(exchanges, live[0].id))
    assert len(await exchanges.list_live(runner.dialog_id)) == 0


def _rendered(event: ConversationEvent) -> str:
    """The user-visible text of an event, for "was this ever shown?" assertions."""
    if isinstance(event.payload, TextDelta):
        return event.payload.text
    if isinstance(event.payload, Finished):
        return event.payload.message.content
    return ""


async def test_late_message_reopens_the_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = GatedLLM()
    router = FakeRouter()
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        llm, quick_registry(), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))

    # the message lands after the process's last sync: the run finishes without it
    router.decide_continue()
    await runner.submit("extra context")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    llm.release.set()
    await collect_completions(queue, 2)

    # the exchange reopens (its answer could not have accounted for the late
    # message) and a fresh run picks it up — durable state instead of the
    # requeue heuristic it replaced
    assert len(llm.requests) == EXPECTED_LLM_CALLS
    third_request = llm.requests[2]
    assert any(
        m.role is MessageRole.USER
        and CLARIFICATION_NOTE_TEMPLATE.format(content="extra context") in m.content
        for m in third_request
    )
    assert [m.content for m in runner.history()] == [
        "start",
        "extra context",
        "first final",
        "after",
    ]


async def test_bring_back_starts_a_new_answer_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM(
        [blocking_call(), reply("second final"), reply("bring-back answer"), reply("first final")]
    )
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")
    events = await collect_until(queue, is_delivered("second final"))
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    # wait for "second" to settle: its question then renders as plain history
    await wait_for_async_condition(lambda: _only_first_is_live(exchanges, runner.dialog_id))
    await runner.submit("bring back the first one")
    events += await collect_until(queue, is_delivered("bring-back answer"))

    # there is no promote route: a "bring back" request is a plain new
    # exchange; its branch sees the whole narrative — "second" is answered
    # history, while "first" is still owed by a live run (dropped)
    bring_back_request = llm.requests[2]
    assert [m.content for m in bring_back_request[1:-1]] == ["second", "second final"]
    assert "bring back the first one" in bring_back_request[-1].content

    tool.release.set()
    events += await collect_until(queue, is_delivered("first final"))
    events += await collect_until(
        queue, lambda e: isinstance(e.payload, ProcessCompleted) and e.payload.title == "first"
    )

    done = completions(events)
    assert {item.title for item in done} == {"first", "second", "bring back the first one"}
    tasks = await store.list(runner.dialog_id)
    await wait_for_condition(lambda: all(t.status is TaskStatus.DONE for t in tasks))
    assert [m.content for m in runner.history()] == [
        "first",
        "second",
        "second final",
        "bring back the first one",
        "bring-back answer",
        "first final",
    ]


async def test_router_cancel_stops_the_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call()])
    router = FakeRouter()
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router, store=store),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    router.action = RouteAction.COMMAND  # a pure stop, nothing to answer
    router.decide_cancel_all()
    await runner.submit("stop it")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    tool.release.set()
    events = await collect_until(queue, is_completed)

    assert any(isinstance(e.payload, Cancelled) for e in events)
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [("start", TaskStatus.CANCELLED.value)]
    # a cancel-routed message is a command, not a question: no re-queue, no answer
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="start"),
        ChatMessage(role=MessageRole.USER, content="stop it"),
    ]
    task = await single_task(store, runner.dialog_id)
    assert task.status is TaskStatus.CANCELLED


async def test_cancel_api_cancels_answer_runs_but_not_run_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stop button stops every live ANSWER run; spawned RUN work survives."""
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = BranchLLM(main=[blocking_call()], background=[reply(TASK_RESULT)])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)

    await runner.cancel()
    events = await collect_until(queue, lambda e: isinstance(e.payload, ProcessCompleted))

    assert any(isinstance(e.payload, Cancelled) for e in events)
    tasks = {task.title: task for task in await store.list(runner.dialog_id)}
    await wait_for_task_state(store, tasks["first"].id, lambda t: t.status is TaskStatus.CANCELLED)
    # the spawned RUN task was not touched by the user's stop
    await wait_for_task_state(store, tasks[TASK_TITLE].id, lambda t: t.status is TaskStatus.DONE)


async def test_process_limit_delivers_a_canned_notice_without_an_llm_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(max_processes=ONE_PROCESS)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("new question")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_REFUSAL)
    assert len(llm.requests) == 1  # the notice needs no LLM run
    notice = runner.history()[2]
    assert notice.role is MessageRole.ASSISTANT
    assert "process limit (1)" in notice.content
    assert "start" in notice.content
    # phase 4: the notice is delivered immediately, its own message
    events = await collect_until(queue, is_delivered(notice.content))
    assert any(isinstance(e.payload, Finished) for e in events)
    tool.release.set()
    events += await collect_until(queue, lambda e: isinstance(e.payload, ProcessCompleted))
    assert len(completions(events)) == 1  # only "start" ever became a process


async def test_process_limit_notice_flushes_immediately_when_foreground_is_free(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    llm.gate_background = True
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    spawned = await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    assert TASK_TITLE in spawned or "task" in spawned

    await runner.submit("new question")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    events = await collect_until(queue, is_delivered(runner.history()[1].content))

    # the slot is taken by the background task: a canned notice, no report run
    notice = runner.history()[1]
    assert notice.role is MessageRole.ASSISTANT
    assert "process limit (1)" in notice.content
    assert TASK_TITLE in notice.content
    assert llm.main_requests == []
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.message.content for item in finished] == [notice.content]

    llm.background_release.set()
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    task = await single_task(store, runner.dialog_id)
    assert task.status is TaskStatus.DONE
    assert task.delivered_at is not None
    assert not runner._pending_deliveries


async def test_spawn_task_refuses_over_the_process_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(max_processes=ONE_PROCESS)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    refusal = await runner.spawn_task("another job", "do it")

    assert "cannot spawn" in refusal
    assert "process limit (1)" in refusal
    assert "start" in refusal

    tool.release.set()
    await collect_until(queue, is_completed)


SELF_DELETE_TOOL = "self_delete"


class SelfDeleteTool:
    """Routes into the real task_delete flow against the only stored task (itself)."""

    def __init__(self, inner: TaskDeleteTool, store: TaskStore) -> None:
        self._inner = inner
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=SELF_DELETE_TOOL, description="quick", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        (task,) = await self._store.list(context.dialog_id)
        return await self._inner.execute({"task_id": task.id}, context)


def self_delete_call() -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=CALL_ID, name=SELF_DELETE_TOOL, arguments={}),),
    )


async def test_delete_task_stops_a_live_background_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[reply("pong")], background=[reply(TASK_RESULT)])
    llm.gate_background = True
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    (task,) = await store.list(runner.dialog_id)

    deletion = asyncio.create_task(runner.delete_task(task.id))
    await asyncio.sleep(0)  # let delete_task reach control.cancel()
    llm.background_release.set()
    outcome = await deletion
    events = await collect_until(queue, is_completed)

    assert outcome is TaskDeleteOutcome.DELETED
    assert [(item.title, item.status) for item in completions(events)] == [
        (TASK_TITLE, TaskStatus.CANCELLED.value)
    ]
    # the stopped task's row is kept as CANCELLED and nothing is delivered
    row = await single_task(store, runner.dialog_id)
    assert row.status is TaskStatus.CANCELLED
    assert not runner._pending_deliveries

    # the actor survived the cancelled task's termination notice
    await runner.submit("ping")
    await collect_completions(queue, 1)
    assert runner.history()[-1].content == "pong"


async def test_delete_task_reports_not_running_for_an_unknown_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(ScriptedLLM([]), ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    assert await runner.delete_task("missing") is TaskDeleteOutcome.NOT_RUNNING


async def test_delete_task_from_inside_its_own_process_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    registry = ToolRegistry()
    inner = TaskDeleteTool(store=store, cron_store=SqlAlchemyCronStore(session_factory))
    registry.register(SelfDeleteTool(inner, store))
    llm = BranchLLM(
        main=[],
        background=[self_delete_call(), reply(TASK_RESULT)],
    )
    manager = make_manager(llm, registry, session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    # the self-deletion was refused: the task ran to completion and was delivered
    by_title = {item.title: item.status for item in completions(events)}
    assert by_title[TASK_TITLE] == TaskStatus.DONE.value
    task = await single_task(store, runner.dialog_id)
    assert task.status is TaskStatus.DONE
    assert task.delivered_at is not None
    assert [m.content for m in runner.history()] == [TASK_RESULT]
    assert runner.history()[0].task_id == task.id


async def test_task_create_tool_runs_background_process_and_delivers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = ToolRegistry()
    registry.register(TaskCreateTool(cron_store=SqlAlchemyCronStore(session_factory)))
    llm = BranchLLM(
        main=[task_create_call(), reply("spawn confirmed")],
        background=[reply(TASK_RESULT)],
    )
    llm.gate_background = True
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, registry, session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("do it in the background")
    events = await collect_completions(queue, 1)
    llm.background_release.set()
    events += await collect_until(queue, is_delivered(TASK_RESULT))

    # the result is delivered verbatim; no report run rephrases it
    assert [m.content for m in runner.history()] == [
        "do it in the background",
        "spawn confirmed",
        TASK_RESULT,
    ]
    tasks = {task.kind: task for task in await store.list(runner.dialog_id)}
    assert tasks[TaskKind.RUN].status is TaskStatus.DONE
    assert tasks[TaskKind.RUN].delivered_at is not None
    assert runner.history()[-1].task_id == tasks[TaskKind.RUN].id
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.message.content for item in finished] == ["spawn confirmed", TASK_RESULT]
    assert len(llm.main_requests) == RETRIED_CALLS  # the tool iteration and the final answer
    background_request = llm.background_requests[0]
    assert background_request[0].content == BACKGROUND_TASK_PROMPT  # no volatile date
    assert background_request[1].role is MessageRole.USER
    assert background_request[1].content.endswith(TASK_PROMPT)  # date envelope + prompt


async def test_run_task_result_delivers_immediately_even_mid_stream(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Phase 4: the delivery gate is gone — a RUN-task result is broadcast the
    moment it finishes, into its own (untagged) message, even while an answer
    is still streaming."""
    tool = BlockingTool()
    llm = BranchLLM(main=[blocking_call(), reply("after")], background=[reply(TASK_RESULT)])
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    # delivered while the answer stream is still blocked in its tool
    events = await collect_until(queue, is_delivered(TASK_RESULT))
    delivered = next(e for e in events if isinstance(e.payload, Finished))
    assert delivered.exchange_id is None  # a RUN result owns no exchange
    (task,) = [t for t in await store.list(runner.dialog_id) if t.kind is TaskKind.RUN]
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert not runner._pending_deliveries

    tool.release.set()
    events += await collect_until(queue, is_delivered("after"))
    # the answer run pulled the background final into its next iteration
    second_request = llm.main_requests[1]
    assert any(TASK_RESULT in m.content for m in second_request)


class FailingMarkDeliveredStore(InMemoryTaskStore):
    """Task store whose first mark_delivered raises, to exercise the flush retry."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = True

    async def mark_delivered(self, task_id: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("store down")
        await super().mark_delivered(task_id)


async def test_delivery_is_retried_when_marking_delivered_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = FailingMarkDeliveredStore()
    llm = BranchLLM(main=[reply("pong")], background=[reply(TASK_RESULT)])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    events = await collect_until(queue, is_delivered(TASK_RESULT))
    # the broadcast happened, but the stamp failed: the delivery stays queued
    (task,) = await store.list(runner.dialog_id)
    assert task.status is TaskStatus.DONE
    assert task.delivered_at is None
    assert len(runner._pending_deliveries) == 1

    # the next flush (any termination with a free foreground) retries it
    await runner.submit("ping")
    events += await collect_until(
        queue, lambda e: isinstance(e.payload, TextDelta) and e.payload.text == TASK_RESULT
    )

    assert task.delivered_at is not None
    assert not runner._pending_deliveries
    # the retry re-sends the events: the transport may see a duplicate
    result_deltas = [e.payload.text for e in events if isinstance(e.payload, TextDelta)].count(
        TASK_RESULT
    )
    assert result_deltas == RETRIED_CALLS


async def test_interrupted_turn_is_salvaged_into_the_narrative(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = StallingLLM()
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("work")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))
    await runner.cancel()
    llm.release.set()
    events = await collect_until(queue, is_completed)

    assert any(isinstance(e.payload, Cancelled) for e in events)
    done = completions(events)
    assert [item.status for item in done] == [TaskStatus.CANCELLED.value]
    history = runner.history()
    assert [message.role for message in history] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.SYSTEM,
    ]
    assert history[1].content == PARTIAL
    assert history[2].content == INTERRUPTED_NOTE
    task = await single_task(store, runner.dialog_id)
    assert task.status is TaskStatus.CANCELLED  # the row is kept, nothing delivered
    assert history[1].task_id == task.id
    dialog = await get_dialog(session_factory)
    assert await SqlAlchemyMessageRepository(session_factory).list(dialog.id) == history


async def test_interrupted_tool_turn_is_salvaged_into_the_narrative(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    llm = ToolStallingLLM()
    manager = make_manager(llm, blocking_registry(tool), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("work")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await runner.cancel()
    llm.release.set()
    tool.release.set()
    events = await collect_until(queue, is_completed)

    assert any(isinstance(e.payload, Cancelled) for e in events)
    done = completions(events)
    assert [item.status for item in done] == [TaskStatus.CANCELLED.value]
    history = runner.history()
    assert [message.role for message in history] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.SYSTEM,
    ]
    assert history[1].content == PARTIAL  # salvaged despite the trailing tool reply
    assert history[2].content == INTERRUPTED_NOTE
    dialog = await get_dialog(session_factory)
    assert await SqlAlchemyMessageRepository(session_factory).list(dialog.id) == history


async def test_narrative_is_rebuilt_after_manager_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM([blocking_call(), reply("after")])
    manager = make_manager(llm, quick_registry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, is_completed)

    restarted = make_manager(ScriptedLLM([]), quick_registry(), session_factory)
    restored = await restarted.get_or_create_runner(USER_ID, CHANNEL)

    assert [m.content for m in restored.history()] == ["start", "after"]
    # the DB load feeds row ids back into the narrative (source_message_id linkage)
    assert all(m.id is not None for m in restored.history())


CRON_JOB_ID = "cron-job-1"
CRON_TITLE = "morning report"
CRON_PROMPT = "prepare the daily report"

#: A second live instance sharing this database.
PEER_NODE = "another-node"


async def test_a_cron_firing_leaves_a_dialog_another_instance_is_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Winning a cron lease is not a reason to move a conversation.

    Every instance sweeps the same jobs table, so whoever polls first sees
    every due job — including jobs of dialogs somebody else is actively
    running. Firing it here would take the dialog, and with two instances
    polling every second a user's conversation would hop between them for no
    reason at all.
    """
    manager = make_manager(BranchLLM(main=[], background=[]), ToolRegistry(), session_factory)
    dialog = await get_dialog(session_factory)
    claims = SqlAlchemyClaimRepository(session_factory)
    held = await claims.claim(dialog.id, PEER_NODE)

    outcome = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)

    assert outcome is WakeOutcome.NOT_OURS
    # the peer still owns it at the same generation: nothing was taken
    assert await claims.current_generation(dialog.id) == held.generation


async def test_a_settled_collection_is_left_to_the_instance_that_owns_the_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same rule for the material sweep, which has no lease at all — so
    without this every instance would grab every settled collection, and the
    dialog would land wherever the last tick happened to run."""
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        BranchLLM(main=[], background=[]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store),
    )
    dialog = await get_dialog(session_factory)
    claims = SqlAlchemyClaimRepository(session_factory)
    held = await claims.claim(dialog.id, PEER_NODE)

    await manager.promote_collection(USER_ID, CHANNEL, "exchange-1")

    assert await claims.current_generation(dialog.id) == held.generation
    assert await store.list(dialog.id) == []  # nothing was started here


async def test_background_work_adopts_a_dialog_nobody_is_running(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Refusing a peer's dialog must not become refusing every dialog: an
    idle user's cron still has to fire somewhere, and an unclaimed dialog is
    exactly what a fresh instance sees for all of them."""
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    dialog = await get_dialog(session_factory)

    outcome = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)

    assert outcome is WakeOutcome.DELIVERED
    assert await SqlAlchemyClaimRepository(session_factory).current_generation(dialog.id)


async def test_a_stale_claim_does_not_hold_background_work_hostage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A claim whose owner died stops being a reason to defer. Otherwise a
    crashed instance would silence its users' cron jobs permanently."""
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(stale_after_seconds=0.0),  # every claim reads as dead
    )
    dialog = await get_dialog(session_factory)
    await SqlAlchemyClaimRepository(session_factory).claim(dialog.id, PEER_NODE)

    outcome = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)

    assert outcome is WakeOutcome.DELIVERED


async def test_wake_runs_cron_tagged_background_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    delivered = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    assert delivered is WakeOutcome.DELIVERED
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    # exactly one delivered message, with the verbatim result and the task link
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert len(finished) == 1
    task = await single_task(store, runner.dialog_id)
    assert finished[0].message == ChatMessage(
        role=MessageRole.ASSISTANT, content=TASK_RESULT, task_id=task.id
    )
    assert task.status is TaskStatus.DONE
    assert task.delivered_at is not None
    assert task.input["cron_job_id"] == CRON_JOB_ID
    assert isinstance(task.input["fired_at"], str)  # the parent linkage timestamp
    assert runner.history() == [finished[0].message]
    assert not runner._pending_deliveries
    assert llm.main_requests == []  # no report run
    background_request = llm.background_requests[0]
    assert background_request[0].content == BACKGROUND_TASK_PROMPT  # no volatile date
    assert background_request[1].role is MessageRole.USER
    assert background_request[1].content.endswith(CRON_PROMPT)  # date envelope + prompt


EXHAUSTED = LimitVerdict(allowed=False, reason="daily_tokens", used=1000, limit=1000)


async def test_exhausted_budget_withholds_the_run_but_keeps_the_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The message is persisted and counted, the exchange stays OPEN with no
    run — once the budget resets, the next trigger picks the work up."""
    gate = RecordingLimitGate()
    gate.run_verdict = EXHAUSTED
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([])  # any LLM call would pop an empty script and fail
    manager = make_manager(
        llm, ToolRegistry(), session_factory, ManagerOptions(store=store, limits=gate)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(
        queue,
        lambda e: isinstance(e.payload, Finished),  # the notice delivery
    )

    notice = next(e.payload for e in events if isinstance(e.payload, Finished))
    assert "daily limit of your plan is exhausted" in notice.message.content
    assert "daily_tokens: 1000/1000" in notice.message.content
    assert next(m.content for m in runner.history()) == "hi"  # persisted, not lost
    assert await store.list(runner.dialog_id) == []  # no run was started
    (exchange,) = await SqlAlchemyExchangeRepository(session_factory).list_live(runner.dialog_id)
    assert exchange.status is ExchangeStatus.OPEN  # the obligation survives
    (counted,) = gate.by_kind(UsageKind.USER_MESSAGE)  # intake always counts
    assert counted.quantity == 1


async def test_exhausted_budget_notices_once_per_dialog_and_day(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gate = RecordingLimitGate()
    gate.run_verdict = EXHAUSTED
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(limits=gate)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first try")
    await collect_until(queue, lambda e: isinstance(e.payload, Finished))
    await runner.submit("second try")
    await wait_for_message(runner, "second try")
    await asyncio.sleep(POLL_SECONDS * 5)  # give a second notice a chance to appear

    notices = [m for m in runner.history() if "daily limit of your plan" in m.content]
    assert len(notices) == 1  # the second refusal the same day stays silent


async def test_exhausted_budget_skips_a_cron_wake_with_one_note_per_day(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gate = RecordingLimitGate()
    gate.run_verdict = EXHAUSTED
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(store=store, limits=gate)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    first = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    events = await collect_until(queue, lambda e: isinstance(e.payload, Finished))
    second = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)

    assert first is WakeOutcome.LIMITED
    assert second is WakeOutcome.LIMITED
    assert await store.list(runner.dialog_id) == []
    notes = [e.payload.message.content for e in events if isinstance(e.payload, Finished)] + [
        m.content for m in runner.history() if CRON_TITLE in m.content
    ]
    assert any("was skipped" in note for note in notes)
    # the second wake the same day stays silent: one note per (job, day)
    skipped_notes = [m for m in runner.history() if "was skipped" in m.content]
    assert len(skipped_notes) == 1


async def test_exhausted_budget_refuses_spawn_task_with_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    gate = RecordingLimitGate()
    gate.run_verdict = EXHAUSTED
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(store=store, limits=gate)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    refusal = await runner.spawn_task(TASK_TITLE, TASK_PROMPT)

    assert "daily limit of the user's plan is exhausted" in refusal
    assert await store.list(runner.dialog_id) == []


async def test_enabled_features_reach_the_tool_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A gated tool disappears from the specs the run offers the model."""
    gate = RecordingLimitGate()
    gate.features = frozenset()  # a plan with no optional features at all
    seen_specs: list[str] = []

    class SpyTool:
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(name="spy", description="records visibility", parameters_schema={})

        def visible_to(self, context: ToolContext) -> bool:
            seen_specs.append(str(context.enabled_features))
            return True

        async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
            return "ok"

    registry = ToolRegistry()
    registry.register(SpyTool())
    manager = make_manager(
        ScriptedLLM([reply()]), registry, session_factory, ManagerOptions(limits=gate)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    await collect_until(queue, is_completed)

    assert seen_specs == ["frozenset()"]


async def test_a_cron_run_is_ledgered_with_cron_origin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    usage = Usage(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS)
    gate = RecordingLimitGate()
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        UsageLLM([reply(TASK_RESULT)], usage),
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store, limits=gate),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    delivered = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    assert delivered is WakeOutcome.DELIVERED
    await collect_until(queue, is_delivered(TASK_RESULT))

    (answer,) = gate.by_kind(UsageKind.LLM_ANSWER)
    task = await single_task(store, runner.dialog_id)
    assert answer.origin is UsageOrigin.CRON
    assert answer.task_id == task.id
    assert answer.quantity == 1
    assert gate.by_kind(UsageKind.USER_MESSAGE) == []  # nobody wrote anything


async def test_wake_delivers_a_failed_event_for_a_failed_cron_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = FailingTaskLLM()
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    delivered = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    assert delivered is WakeOutcome.DELIVERED
    events = await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    failed = [e.payload for e in events if isinstance(e.payload, Failed)]
    assert len(failed) == 1
    assert PROVIDER_ERROR_MESSAGE in failed[0].error
    task = await single_task(store, runner.dialog_id)
    assert task.status is TaskStatus.FAILED
    assert PROVIDER_ERROR_MESSAGE in (task.error or "")
    assert task.delivered_at is not None
    assert runner.history() == []  # a failure leaves no message in the history


async def test_two_cron_results_are_delivered_separately(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply("report one"), reply("report two")])
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.wake(USER_ID, CHANNEL, "first job", CRON_PROMPT, CRON_JOB_ID)
    await manager.wake(USER_ID, CHANNEL, "second job", CRON_PROMPT, "cron-job-2")
    events = await collect_completions(queue, 2)
    events += await collect_until(queue, is_delivered("report two"))

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert {item.message.content for item in finished} == {"report one", "report two"}
    tasks = await store.list(runner.dialog_id)
    assert {task.title for task in tasks} == {"first job", "second job"}
    assert all(task.status is TaskStatus.DONE for task in tasks)
    await wait_for_condition(lambda: all(task.delivered_at is not None for task in tasks))
    # one task = one message, each linked to its producer
    by_task = {task.id: task for task in tasks}
    history = runner.history()
    assert {message.content for message in history} == {"report one", "report two"}
    assert all(message.task_id in by_task for message in history)


async def test_wake_over_the_process_limit_publishes_a_canned_notice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    delivered = await runner.wake(CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    assert delivered is False

    note = runner.history()[-1]
    assert note.role is MessageRole.ASSISTANT
    assert f"Cron job '{CRON_TITLE}' could not start" in note.content
    assert "process limit (1)" in note.content
    assert "start" in note.content
    # no RUN task was created for the refused cron firing (the answer task of
    # the foreground question is the only row)
    assert [task for task in await store.list(runner.dialog_id) if task.kind is TaskKind.RUN] == []

    # phase 4: the notice delivers immediately, no gate to wait behind
    events = await collect_until(queue, is_delivered(note.content))
    tool.release.set()
    events += await collect_until(queue, is_delivered("after"))
    deltas = [e.payload.text for e in events if isinstance(e.payload, TextDelta)]
    assert deltas == [note.content, "after"]
    assert len(llm.requests) == RETRIED_CALLS  # the notice needed no LLM run


class RecordingOutcomeListener:
    """TaskOutcomeListener stub recording (task, status) pairs; can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[Task, TaskStatus]] = []
        self.fail = fail

    async def report_outcome(self, task: Task, status: TaskStatus) -> None:
        if self.fail:
            raise RuntimeError("listener boom")
        self.calls.append((task, status))


async def test_cron_task_outcome_is_reported_to_the_listener(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    listener = RecordingOutcomeListener()
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(listener=listener),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    await collect_completions(queue, 1)

    (reported,) = listener.calls
    task, status = reported
    assert status is TaskStatus.DONE
    assert task.input["cron_job_id"] == CRON_JOB_ID


async def test_plain_task_outcome_is_not_reported(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    listener = RecordingOutcomeListener()
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(listener=listener),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    await collect_completions(queue, 1)

    assert listener.calls == []  # no cron_job_id in the task input


async def test_listener_failure_does_not_break_finalize(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store, listener=RecordingOutcomeListener(fail=True)),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    await collect_until(queue, is_delivered(TASK_RESULT))

    # finalize and delivery completed despite the listener failure
    task = await single_task(store, runner.dialog_id)
    assert task.status is TaskStatus.DONE
    assert task.delivered_at is not None
    assert [m.content for m in runner.history()] == [TASK_RESULT]


async def test_actor_survives_a_failing_command(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seen = {"calls": 0}

    class BoomingRouter:
        """Raises on its first call, then behaves."""

        async def route(
            self,
            exchanges: tuple[ExchangeInfo, ...],
            message: str,
            max_exchanges: int,
        ) -> RouteDecision:
            seen["calls"] += 1
            if seen["calls"] == FIRST_CALL:
                raise RuntimeError("router boom")
            return RouteDecision()

    tool = BlockingTool()
    manager = make_manager(
        ScriptedLLM([blocking_call(), reply(), reply()]),
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=BoomingRouter()),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    # the first message needs no router; the next two reach it while the first
    # exchange is still live — one raises, and the actor must survive it
    await runner.submit("first")
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")  # router raises: the actor logs and stays alive
    await runner.submit("third")  # processed normally, proving the actor survived
    tool.release.set()

    events = await collect_completions(queue, 2)
    assert TaskStatus.DONE.value in [item.status for item in completions(events)]
    assert seen["calls"] == SECOND_CALL


class ConvertingRouter:
    """MessageRouter stub turning a cancellation into a plain error.

    Mirrors what a failing store does to an in-flight CancelledError (e.g.
    SQLAlchemy raising OperationalError from a disposed engine): the error
    escaping the dispatch is a regular Exception, so the actor's broad
    except used to swallow the cancellation and loop forever.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        self.entered.set()
        try:
            await asyncio.Event().wait()  # blocks until the actor is cancelled
        except asyncio.CancelledError:
            raise RuntimeError("converted cancellation") from None
        raise AssertionError("unreachable: the wait only ends via cancellation")


async def test_actor_dies_when_a_failing_command_races_its_cancellation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router = ConvertingRouter()
    tool = BlockingTool()
    manager = make_manager(
        ScriptedLLM([blocking_call(), reply()]),
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    await runner.submit("hi")  # no live exchange yet: the router is skipped
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("again")  # now the router is reached — and blocks
    await asyncio.wait_for(router.entered.wait(), timeout=TIMEOUT_SECONDS)
    tool.release.set()
    await runner.stop()  # cancels the actor mid-dispatch

    actor = runner._actor_task
    assert actor is not None
    await wait_for_condition(actor.done)
    assert actor.cancelled()


class FailingFinalizeStore(InMemoryTaskStore):
    """Task store whose first mark_done raises, to exercise finalize failure."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_mark_done = True

    async def mark_done(self, task: Task, result: str) -> None:
        if self.fail_next_mark_done:
            self.fail_next_mark_done = False
            raise RuntimeError("store unavailable")
        await super().mark_done(task, result)


async def test_process_slot_released_when_finalize_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = FailingFinalizeStore()
    manager = make_manager(
        ScriptedLLM([reply("bg1"), reply("bg2")]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.spawn_task("t1", "p1")  # finalize fails inside mark_done
    await collect_completions(queue, 1)  # completion is still announced

    # the slot must be freed despite the finalize failure: a second spawn is accepted
    result = await runner.spawn_task("t2", "p2")
    assert "spawned" in result
    assert "process limit" not in result
    await collect_completions(queue, 1)


class GatedTaskStore(InMemoryTaskStore):
    """Task store pausing `add` until released, to force a spawn race."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def add(self, task: Task) -> None:
        self.entered.set()
        await self.release.wait()
        await super().add(task)


async def test_concurrent_spawns_do_not_exceed_the_process_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = GatedTaskStore()
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    first = asyncio.create_task(runner.spawn_task("job a", "do a"))
    await asyncio.wait_for(store.entered.wait(), timeout=TIMEOUT_SECONDS)
    second = asyncio.create_task(runner.spawn_task("job b", "do b"))
    await asyncio.sleep(0)  # let the second spawn reach the spawn lock
    await asyncio.sleep(0)
    store.release.set()
    results = {await first, await second}

    spawned = [result for result in results if "spawned" in result]
    refused = [result for result in results if "cannot spawn" in result]
    assert len(spawned) == 1
    assert len(refused) == 1
    assert "process limit (1)" in refused[0]
    tasks = await store.list(runner.dialog_id)
    # the loser is refused before storing anything under the spawn lock
    assert [task.status for task in tasks] == [TaskStatus.RUNNING]

    await collect_completions(queue, 1)


class GatedCompactor:
    """ContextCompactor stub pausing assemble until released, to force a spawn race."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        self.entered.set()
        await self.release.wait()
        return AssembledContext(
            messages=list(history), tail_count=len(history), snapshot_len=len(history)
        )

    async def compacted_boundary(self, dialog_id: str) -> int:
        return 0

    async def compact_now(self, dialog: Dialog) -> bool:
        return True

    async def aclose(self, dialog_id: str) -> None:
        pass


async def test_spawn_during_a_pending_start_new_does_not_exceed_the_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    compactor = GatedCompactor()
    llm = BranchLLM(main=[reply("answer")], background=[])
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(compactor=compactor, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hello")  # the default empty route decision maps to START_NEW
    await asyncio.wait_for(compactor.entered.wait(), timeout=TIMEOUT_SECONDS)
    spawn = asyncio.create_task(runner.spawn_task(TASK_TITLE, TASK_PROMPT))
    await asyncio.sleep(0)  # let the spawn reach the spawn lock
    compactor.release.set()
    result = await spawn

    # the actor's foreground run claimed the single slot while the spawn waited
    assert "cannot spawn" in result
    assert "process limit (1)" in result
    await collect_completions(queue, 1)


# --- graceful shutdown ---------------------------------------------------------


async def test_stop_cancels_and_awaits_actor_and_pumps(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    llm = ToolStallingLLM()
    manager = make_manager(llm, blocking_registry(tool), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("work")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))

    # the pump is stalled mid-stream: stop must cancel it directly, not hang
    await runner.stop()
    llm.release.set()
    tool.release.set()

    actor = runner._actor_task
    assert actor is not None
    assert actor.done()  # awaited by stop, not left to die with the event loop
    assert runner._processes == {}  # every pump was awaited and terminated


async def test_stop_closes_only_the_own_dialogs_compaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    compactor = FakeCompactor()
    manager = make_manager(
        ScriptedLLM([reply()]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(compactor=compactor),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    await runner.stop()

    assert compactor.closed == [runner.dialog_id]


async def test_stop_all_stops_and_deregisters_every_runner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(ScriptedLLM([reply()]), ToolRegistry(), session_factory)
    first = await manager.get_or_create_runner(USER_ID, CHANNEL)
    second = await manager.get_or_create_runner("user-2", CHANNEL)

    await manager.stop_all()

    for runner in (first, second):
        actor = runner._actor_task
        assert actor is not None
        assert actor.done()
    # the registry was cleared: a late request builds a fresh runner
    assert await manager.get_or_create_runner(USER_ID, CHANNEL) is not first


# --- startup recovery ----------------------------------------------------------


async def add_exchange_task(
    session_factory: async_sessionmaker[AsyncSession],
    exchange_id: str,
    task_id: str,
    created_at: datetime | None = None,
) -> Task:
    """Store a RUNNING answer task of the exchange — ownership, as derived.

    "Who owns the exchange" is its newest task now, so a test that used to
    poke an owner id into the exchange row instead records the task that
    would have put it there.
    """
    dialog = await get_dialog(session_factory)
    task = Task(
        id=task_id,
        dialog_id=dialog.id,
        title=task_id,
        kind=TaskKind.ANSWER,
        exchange_id=exchange_id,
        input={"prompt": task_id, "exchange_id": exchange_id},
        status=TaskStatus.RUNNING,
        created_at=created_at if created_at is not None else utc_now(),
        started_at=utc_now(),
    )
    await SqlAlchemyTaskStore(session_factory).add(task)
    return task


def orphaned_task(dialog: Dialog, cron_job_id: str | None = None, **overrides: object) -> Task:
    """Build a RUN task as a previous instance left it: stored and running."""
    task_input: dict[str, Any] = {"title": TASK_TITLE, "prompt": TASK_PROMPT}
    if cron_job_id is not None:
        task_input["cron_job_id"] = cron_job_id
    task = Task(
        dialog_id=dialog.id,
        title=TASK_TITLE,
        kind=TaskKind.RUN,
        input=task_input,
        status=TaskStatus.RUNNING,
        started_at=utc_now(),
    )
    return replace(task, **overrides) if overrides else task


async def test_recover_interrupted_restarts_orphaned_run_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([reply(TASK_RESULT)])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog)
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.DONE  # the restarted process finished it
    assert stored.delivered_at is not None
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [(TASK_TITLE, TaskStatus.DONE.value)]
    assert isinstance(events[-1].payload, Finished)
    # the RUN restart branch is self-contained: background prompt + task prompt
    request = llm.requests[0]
    assert request[0].content == BACKGROUND_TASK_PROMPT
    assert request[1].role is MessageRole.USER
    assert request[1].content.endswith(TASK_PROMPT)


async def test_recover_interrupted_restarts_orphaned_answer_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([reply("re-answer")])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    # the question lives in the narrative, persisted before the crash
    await SqlAlchemyMessageRepository(session_factory).append(
        dialog.id, ChatMessage(role=MessageRole.USER, content="unanswered question")
    )
    task = orphaned_task(
        dialog,
        kind=TaskKind.ANSWER,
        title="unanswered question",
        input={"prompt": "unanswered question"},
    )
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    await collect_until(queue, is_delivered("re-answer"))

    assert (await store.get(task.id)).status is TaskStatus.DONE
    # the ANSWER restart branch re-attaches to the narrative: system + snapshot
    request = llm.requests[0]
    assert request[0] == ChatMessage(role=MessageRole.SYSTEM, content=PROMPT)
    assert request[1].role is MessageRole.USER
    assert request[1].content.endswith("unanswered question")


async def test_recover_interrupted_reports_cron_outcome_after_the_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    listener = RecordingOutcomeListener()
    llm = ScriptedLLM([reply("one"), reply("two")])
    manager = make_manager(
        llm,
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store, listener=listener),
    )
    dialog = await get_dialog(session_factory)
    cron_task = orphaned_task(dialog, cron_job_id="job-1")
    plain_task = orphaned_task(dialog)
    for task in (cron_task, plain_task):
        await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    await collect_completions(queue, 2)

    # the restarted tasks report through the normal finalize path: only the
    # cron-tagged one reaches the listener
    assert [(task.id, status) for task, status in listener.calls] == [
        (cron_task.id, TaskStatus.DONE)
    ]


async def test_queued_answer_knows_its_question_after_being_overtaken(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Prod incident 2026-07-28: two quick questions, only the first answered.

    The router (an LLM call) still held the second message when the foreground
    finished. Under the old model the branch ended with the foreground's
    ANSWER sitting after its own question, the task was implied by position,
    and the run produced an empty final that the silent-done path swallowed.
    Now the message joins the narrative only once its exchange is known, and
    the branch states the task outright.
    """
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), reply("seagull answer"), reply("weather answer")])
    router = GatedRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("why so many seagulls?")
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    router.gate_armed = True
    await runner.submit("what is the weather?")
    await asyncio.wait_for(router.entered.wait(), timeout=TIMEOUT_SECONDS)

    # the foreground finishes while the router still holds the second message
    tool.release.set()
    await collect_until(queue, is_completed)
    router.release.set()
    events = await collect_until(queue, is_delivered("weather answer"))

    # the message became visible only after routing, so it sits AFTER the
    # foreground's final — and it is marked as this run's task either way
    queued_branch = llm.requests[-1]
    assert any(
        TASK_NOTE_TEMPLATE.format(content="what is the weather?") in m.content
        for m in queued_branch
    )
    assert any(m.content.endswith("seagull answer") for m in queued_branch)
    # and the user actually gets the second answer
    assert any(isinstance(e.payload, Finished) for e in events)
    tasks = {task.title: task for task in await store.list(runner.dialog_id)}
    weather = tasks["what is the weather?"]
    await wait_for_task_state(store, weather.id, lambda t: t.result == "weather answer")
    await wait_for_condition(lambda: [m.content for m in runner.history()][-1] == "weather answer")


async def test_cancel_takes_absorbed_clarifications_with_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Characterization of the documented tradeoff: a clarification the run
    already absorbed (synced with the mid-run note) dies with a cancel — the
    user cancelled the whole exchange, and the requeue net only covers what
    arrived after the LAST sync."""
    tool = TwoPhaseBlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), blocking_call(), reply("never")])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await asyncio.wait_for(tool.started[0].wait(), timeout=TIMEOUT_SECONDS)
    router.decide_continue()
    await runner.submit("уточнение")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    tool.release[0].set()  # iteration 2: the sync absorbs the clarification
    await asyncio.wait_for(tool.started[1].wait(), timeout=TIMEOUT_SECONDS)
    assert any(
        CLARIFICATION_NOTE_TEMPLATE.format(content="уточнение") in m.content
        for m in llm.requests[1]
    )

    await runner.cancel()
    events = await collect_until(queue, is_completed)
    tool.release[1].set()

    (task,) = await store.list(runner.dialog_id)
    assert task.status is TaskStatus.CANCELLED
    # the absorbed clarification is NOT re-routed: no second answer process
    completed = completions(events)
    assert [item.status for item in completed] == [TaskStatus.CANCELLED.value]
    assert len(router.calls) == 1  # only the clarification needed routing
    assert not any(isinstance(e.payload, Finished) for e in events)


async def test_a_failed_answer_does_not_affect_the_sibling_stream(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One run failing on the provider leaves the other exchange untouched."""
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ExhaustedFailsLLM([blocking_call(), reply("second final")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")
    events = await collect_until(queue, is_delivered("second final"))
    tool.release.set()  # the first run's iteration 2 hits the dry script -> Failed
    events += await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    tasks = {task.title: task for task in await store.list(runner.dialog_id)}
    await wait_for_task_state(store, tasks["first"].id, lambda t: t.status is TaskStatus.FAILED)
    await wait_for_task_state(store, tasks["second"].id, lambda t: t.status is TaskStatus.DONE)


async def test_router_cancel_targets_one_exchange_while_the_other_streams(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """CANCEL(exchange) stops one obligation; the other keeps streaming."""
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), blocking_call(), reply("first final")])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await runner.submit("second")
    await wait_for_condition(lambda: len(runner._processes) == TWO_PROCESSES)
    second = next(p for p in runner._processes.values() if p.title == "second")
    assert second.exchange_id is not None

    router.action = RouteAction.COMMAND
    router.decide_cancel(second.exchange_id)
    await runner.submit("cancel the second one")
    await wait_for_condition(lambda: len(runner._processes) == 1)
    tool.release.set()
    events = await collect_until(queue, is_delivered("first final"))

    tasks = {task.title: task for task in await store.list(runner.dialog_id)}
    await wait_for_task_state(store, tasks["second"].id, lambda t: t.status is TaskStatus.CANCELLED)
    await wait_for_task_state(store, tasks["first"].id, lambda t: t.status is TaskStatus.DONE)
    assert any(
        isinstance(e.payload, Finished) and e.payload.message.content == "first final"
        for e in events
    )


async def test_redelivery_threads_the_reply_and_skips_silent_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Recovery redeliveries keep reply threading and never redeliver emptiness."""
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    threaded = orphaned_task(dialog, status=TaskStatus.DONE, result=TASK_RESULT)
    threaded.input["source_client_message_id"] = "777"
    silent = orphaned_task(dialog, status=TaskStatus.DONE, result="", title="silent")
    await store.add(threaded)
    await store.add(silent)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    started = [e.payload for e in events if isinstance(e.payload, ProcessStarted)]
    assert [item.source_client_message_id for item in started] == ["777"]
    # the empty result was stamped delivered without any delivery events
    await wait_for_task_state(store, silent.id, lambda t: t.delivered_at is not None)
    assert not any(isinstance(e.payload, TextDelta) and e.payload.text == "" for e in events)
    assert not runner._pending_deliveries


async def test_failed_delivery_carries_the_reply_target(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A FAILED redelivery opens with ProcessStarted too: errors thread like answers."""
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog, status=TaskStatus.FAILED, error=PROVIDER_ERROR_MESSAGE)
    task.input["source_client_message_id"] = "888"
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    events = await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    kinds = [type(e.payload) for e in events]
    assert kinds.index(ProcessStarted) < kinds.index(Failed)
    started = next(e.payload for e in events if isinstance(e.payload, ProcessStarted))
    assert started.source_client_message_id == "888"


async def test_recover_interrupted_redelivers_undelivered_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog, status=TaskStatus.DONE, result=TASK_RESULT)
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert finished[0].message == ChatMessage(
        role=MessageRole.ASSISTANT, content=TASK_RESULT, task_id=task.id
    )
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert llm.requests == []  # redelivery needs no LLM run


async def test_recover_interrupted_redelivers_undelivered_failures(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog, status=TaskStatus.FAILED, error=PROVIDER_ERROR_MESSAGE)
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    events = await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    failed = [e.payload for e in events if isinstance(e.payload, Failed)]
    assert failed[0].error == PROVIDER_ERROR_MESSAGE
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert runner.history() == []


async def test_recover_interrupted_skips_already_delivered_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog, status=TaskStatus.DONE, result=TASK_RESULT)
    task.delivered_at = utc_now()
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    await asyncio.sleep(POLL_SECONDS * 5)  # give the actor a chance to misbehave

    assert queue.empty()  # delivered already: no events, no LLM calls
    assert llm.requests == []
    assert runner.history() == []


async def test_redelivery_without_a_subscriber_waits_instead_of_being_stamped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The startup sweep runs before the surfaces attach (see `main.runtime`).

    Broadcasting into zero subscriber queues reaches nobody, so stamping the
    task delivered there would drop the result for good: the next sweep skips
    a stamped row, and a push surface (Telegram) shows nothing.
    """
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog, status=TaskStatus.DONE, result=TASK_RESULT)
    await store.add(task)

    await manager.recover_interrupted()
    await asyncio.sleep(POLL_SECONDS * 5)  # give the actor a chance to misbehave

    assert task.delivered_at is None
    assert await store.list_undelivered() == [task]

    # the transport attaches (an SSE client connects, a Telegram bridge warms)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert finished[0].message == ChatMessage(
        role=MessageRole.ASSISTANT, content=TASK_RESULT, task_id=task.id
    )
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert llm.requests == []  # redelivery needs no LLM run


async def test_cron_result_of_an_unwatched_dialog_waits_for_a_subscriber(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A firing whose dialog has no transport attached keeps its result queued."""
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        BranchLLM(main=[], background=[reply(TASK_RESULT)]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store),
    )

    fired = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    assert fired is WakeOutcome.DELIVERED
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    task = await single_task(store, runner.dialog_id)
    task = await wait_for_task_state(store, task.id, lambda t: t.status is TaskStatus.DONE)
    await asyncio.sleep(POLL_SECONDS * 5)

    assert task.delivered_at is None
    assert runner._pending_deliveries != []  # the outbox holds it

    queue = runner.subscribe()
    await collect_until(queue, is_delivered(TASK_RESULT))

    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert not runner._pending_deliveries


async def test_recover_interrupted_noop_without_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory)

    await manager.recover_interrupted()

    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    assert runner.history() == []
    assert llm.requests == []


class SweepFailingStore(InMemoryTaskStore):
    """Task store failing the orphaned sweep (database outage at startup)."""

    async def list_orphaned(self) -> list[Task]:
        raise RuntimeError("database down")


async def test_recover_interrupted_survives_store_failures(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(
        ScriptedLLM([]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=SweepFailingStore()),
    )

    await manager.recover_interrupted()  # a recovery failure must not take the app down


async def test_restart_task_over_the_limit_fails_the_task_and_delivers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), reply("after")])
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog)
    await store.add(task)
    await runner.restart_task(task)  # the single slot is taken

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.FAILED
    assert stored.error == RESTART_LIMIT_ERROR

    tool.release.set()
    events = await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    failed = [e.payload for e in events if isinstance(e.payload, Failed)]
    assert any(item.error == RESTART_LIMIT_ERROR for item in failed)
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)


async def test_request_result_delivery_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog, status=TaskStatus.DONE, result=TASK_RESULT)
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    runner.request_result_delivery(task.id)
    runner.request_result_delivery(task.id)  # a repeated sweep must not duplicate
    events = await collect_until(queue, is_delivered(TASK_RESULT))
    await asyncio.sleep(POLL_SECONDS * 5)

    assert (await store.get(task.id)).delivered_at is not None
    assert queue.empty()
    delivered = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert len(delivered) == 1


# --- slow-subscriber broadcast policy (C3) ----------------------------------


async def test_full_subscriber_queue_never_drops_terminal_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A laggy transport loses stream chatter, never a result.

    Terminal events gate `delivered_at`: dropping one would mean the store
    says delivered while no transport ever saw the result. The oldest queued
    event is evicted instead.
    """
    manager = make_manager(ScriptedLLM([]), quick_registry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    for _ in range(SUBSCRIBER_QUEUE_SIZE + 5):  # flood the queue past its capacity
        runner._broadcast(TextDelta(text="x"))

    final = Finished(message=ChatMessage(role=MessageRole.ASSISTANT, content=REPLY))
    accepted = runner._broadcast(final)

    assert accepted == 1
    payloads = []
    while not queue.empty():
        payloads.append(queue.get_nowait().payload)
    finals = [p for p in payloads if isinstance(p, Finished)]
    assert len(finals) == 1 and finals[0].message.content == REPLY
    await manager.stop_all()


async def test_full_subscriber_queue_drops_stream_chatter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(ScriptedLLM([]), quick_registry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    for _ in range(SUBSCRIBER_QUEUE_SIZE):
        runner._broadcast(TextDelta(text="x"))

    accepted = runner._broadcast(TextDelta(text="overflow"))

    assert accepted == 0
    assert queue.qsize() == SUBSCRIBER_QUEUE_SIZE  # nothing evicted for a delta
    await manager.stop_all()


class StuckRouter:
    """MessageRouter stub hanging until released (a slow routing LLM call)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        if exchanges:  # the first message needs no router (nothing to belong to)
            self.entered.set()
            await self.release.wait()
        return RouteDecision()


async def test_cancel_bypasses_an_actor_stuck_in_routing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stop must act now, not queue behind a routing LLM call (C2).

    The actor is busy routing the second message (a call that may take up to
    the router timeout); the user's cancel still stops the foreground
    immediately because it never goes through the inbox.
    """
    tool = BlockingTool()
    router = StuckRouter()
    llm = ScriptedLLM([blocking_call()])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")  # the actor enters the stuck router call
    await asyncio.wait_for(router.entered.wait(), timeout=TIMEOUT_SECONDS)

    await runner.cancel()
    events = await collect_until(queue, lambda e: isinstance(e.payload, Cancelled))

    assert any(isinstance(e.payload, Cancelled) for e in events)
    router.release.set()
    await manager.stop_all()


# --- per-dialog runner initialization (S2) -----------------------------------


class SlowHistoryRepository(SqlAlchemyMessageRepository):
    """MessageRepository whose history load blocks until released."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.release = asyncio.Event()
        self.loads = 0

    async def list_hot_slice(self, dialog_id: str) -> list[ChatMessage]:
        self.loads += 1
        if self.loads == 1:  # only the FIRST contact is slow
            await self.release.wait()
        return await super().list_hot_slice(dialog_id)


async def test_one_dialogs_slow_init_does_not_block_another(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The manager lock guards the build map only, not the DB work (S2).

    Previously one global lock was held across the dialog row and the full
    history load: the slowest first contact serialized every dialog in the
    process.
    """
    slow = SlowHistoryRepository(session_factory)
    manager = make_manager(ScriptedLLM([reply()]), quick_registry(), session_factory)
    manager._messages = slow

    first = asyncio.create_task(manager.get_or_create_runner(USER_ID, CHANNEL))
    await wait_for_condition(lambda: slow.loads == 1)  # first build is stuck loading

    # a different dialog must come up while the first is still loading
    other = await asyncio.wait_for(
        manager.get_or_create_runner("user-2", CHANNEL), timeout=TIMEOUT_SECONDS
    )
    assert other.dialog_id != ""

    slow.release.set()
    runner = await asyncio.wait_for(first, timeout=TIMEOUT_SECONDS)
    assert runner is not other
    await manager.stop_all()


async def test_concurrent_first_contacts_share_one_build(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slow = SlowHistoryRepository(session_factory)
    slow.release.set()
    manager = make_manager(ScriptedLLM([reply()]), quick_registry(), session_factory)
    manager._messages = slow

    runners = await asyncio.gather(
        *(manager.get_or_create_runner(USER_ID, CHANNEL) for _ in range(5))
    )

    assert len({id(runner) for runner in runners}) == 1
    assert slow.loads == 1  # one build, not five (the first load runs unreleased)
    await manager.stop_all()


# --- hot-tail narrative (S3) --------------------------------------------------


class TailCompactor:
    """ContextCompactor stub keeping only the last `tail` narrative messages."""

    def __init__(self, tail: int, boundary: int = 0) -> None:
        self.tail = tail
        self.boundary = boundary

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        kept = list(history[-self.tail :]) if self.tail else []
        return AssembledContext(messages=kept, tail_count=len(kept), snapshot_len=len(history))

    async def compacted_boundary(self, dialog_id: str) -> int:
        return self.boundary

    async def compact_now(self, dialog: Dialog) -> bool:
        return False

    async def aclose(self, dialog_id: str) -> None:
        pass


async def test_narrative_trims_to_the_hot_tail(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Compacted messages leave memory: the narrative mirrors the hot tail only (S3)."""
    llm = ScriptedLLM([reply(), reply(SECOND_REPLY)])
    manager = make_manager(
        llm,
        quick_registry(),
        session_factory,
        ManagerOptions(compactor=TailCompactor(tail=1)),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_completions(queue, 1)
    await runner.submit("second")
    await collect_completions(queue, 1)

    # at the second start the assembled tail was just ["second"]: everything
    # older was dropped from memory; the new final still appends normally
    contents = [message.content for message in runner.history()]
    assert contents == ["second", SECOND_REPLY]
    await manager.stop_all()


async def test_initial_narrative_load_starts_after_the_compaction_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A runner never loads compacted history into memory (S3).

    The boundary comes from the summaries themselves, in the same statement
    that reads the messages — so this seeds a real summary covering the first
    two rather than stubbing a number. What is compacted is a fact about the
    data, not a policy the actor asks about separately.
    """
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)
    for content in ("old-1", "old-2", "hot-3"):
        await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content=content))
    await SqlAlchemySummaryStore(session_factory).create(
        DialogueSummary(
            id="summary-1",
            dialog_id=dialog.id,
            seq_from=1,
            seq_to=2,
            topics=("old",),
            content="the first two, compacted",
            created_at=utc_now(),
        )
    )
    manager = make_manager(
        ScriptedLLM([]),
        quick_registry(),
        session_factory,
        ManagerOptions(compactor=TailCompactor(tail=10)),
    )

    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    assert [message.content for message in runner.history()] == ["hot-3"]
    await manager.stop_all()


async def test_trim_narrative_remaps_watermarks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """White-box: trimming shifts process watermarks (ownership lives in the DB)."""
    tool = BlockingTool()
    llm = ScriptedLLM([blocking_call()])
    manager = make_manager(llm, blocking_registry(tool), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    runner.subscribe()
    await runner.submit("first")
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    process = next(iter(runner._processes.values()))
    watermark_before = process.watermark
    (first_message,) = runner.history()
    assert first_message.id is not None

    runner._trim_narrative(len(runner.history()))  # everything compacted away

    assert runner.history() == []
    assert process.watermark == 0 and watermark_before > 0

    tool.release.set()
    await manager.stop_all()


class FailingAppendRepository(SqlAlchemyMessageRepository):
    """MessageRepository whose append always fails (a broken store)."""

    async def append(
        self,
        dialog_id: str,
        message: ChatMessage,
        usage: Usage | None = None,
        client_message_id: str | None = None,
    ) -> str:
        raise RuntimeError("store down")


async def test_failed_submit_persist_reports_to_the_transport(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A store failure must not swallow the user's message silently.

    Previously the actor's catch only logged it: the user watched an answer
    that would never come. Now the transport receives a Failed event telling
    them to resend.
    """
    manager = make_manager(ScriptedLLM([reply()]), quick_registry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    runner._messages = FailingAppendRepository(session_factory)
    queue = runner.subscribe()

    await runner.submit("hello?")
    events = await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    failed = next(e.payload for e in events if isinstance(e.payload, Failed))
    assert failed.error == SUBMIT_FAILED_ERROR
    assert runner.history() == []  # nothing was recorded, nothing routed
    await manager.stop_all()


# --- exchange settlement guards, recovery and reply routing ------------------


async def test_settle_skips_when_the_exchange_changed_hands(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A termination from a task that no longer owns the exchange must not
    clobber the new owner's IN_PROGRESS state (`_settle_exchange`'s owner guard)."""
    manager = make_manager(ScriptedLLM([]), ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    exchange = await exchanges.create(runner.dialog_id, "title", status=ExchangeStatus.IN_PROGRESS)
    # the dead task is older; the follow-up's task is the exchange's newest,
    # which is what owning it now means
    await add_exchange_task(
        session_factory, exchange.id, DEAD_TASK_ID, created_at=utc_now() - timedelta(minutes=1)
    )
    await add_exchange_task(session_factory, exchange.id, OTHER_TASK_ID)

    await runner._settle_exchange(
        _ProcessTerminated(
            task_id=DEAD_TASK_ID,
            exchange_id=exchange.id,
            exchange_status=ExchangeStatus.ANSWERED,
            unseen_messages=_Unseen.SPOKEN,
        )
    )

    settled = await exchanges.get(exchange.id)
    assert settled.status is ExchangeStatus.IN_PROGRESS
    assert runner._processes == {}  # no reopen, no new process
    await manager.stop_all()


async def test_settle_does_not_resurrect_a_cancelled_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A dead task's late termination, even with unseen messages, must not
    bring a CANCELLED exchange back to life.

    The dead task is stored and IS the exchange's newest — a user cancel
    settles the exchange without spawning anything, so the cancelled run
    keeps that distinction. What must stop it is the exchange no longer
    being live, not the identity check.
    """
    manager = make_manager(ScriptedLLM([]), ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    exchange = await exchanges.create(runner.dialog_id, "title")
    await add_exchange_task(session_factory, exchange.id, DEAD_TASK_ID)
    await exchanges.set_status(exchange.id, ExchangeStatus.CANCELLED)

    await runner._settle_exchange(
        _ProcessTerminated(
            task_id=DEAD_TASK_ID,
            exchange_id=exchange.id,
            exchange_status=ExchangeStatus.ANSWERED,
            unseen_messages=_Unseen.SPOKEN,
        )
    )

    settled = await exchanges.get(exchange.id)
    assert settled.status is ExchangeStatus.CANCELLED
    assert runner._processes == {}
    await manager.stop_all()


async def test_router_cancel_settles_the_exchange_as_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call()])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    router.action = RouteAction.COMMAND
    router.decide_cancel_all()
    await runner.submit("stop it")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)

    maybe_exchange_id = runner.history()[0].exchange_id
    assert maybe_exchange_id is not None
    exchange_id: str = maybe_exchange_id
    await wait_for_async_condition(lambda: _is_cancelled(exchanges, exchange_id))

    tool.release.set()
    await collect_until(queue, is_completed)


async def test_failed_run_settles_its_exchange_as_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyTaskStore(session_factory)
    llm = ExhaustedFailsLLM([])  # the very first stream call fails
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("boom")
    await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    maybe_exchange_id = runner.history()[0].exchange_id
    assert maybe_exchange_id is not None
    exchange_id: str = maybe_exchange_id
    await wait_for_async_condition(lambda: _is_failed(exchanges, exchange_id))


async def test_recover_interrupted_reopens_a_stranded_in_progress_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(ScriptedLLM([]), ToolRegistry(), session_factory)
    dialog = await get_dialog(session_factory)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    exchange = await exchanges.create(dialog.id, "stranded", status=ExchangeStatus.IN_PROGRESS)

    await manager.recover_interrupted()

    reopened = await exchanges.get(exchange.id)
    assert reopened.status is ExchangeStatus.OPEN


async def test_recover_interrupted_leaves_an_awaiting_user_answer_task_silently_done(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ANSWER task that died right after `ask_user` (the ask went out, the
    exchange is AWAITING_USER) must not be restarted: a restarted run would
    duplicate the work and clobber the parked question. `_start_orphaned`
    closes the row silently instead; the user's reply resumes the exchange."""
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    dialog = await get_dialog(session_factory)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    exchange = await exchanges.create(dialog.id, "which city?")
    await exchanges.set_status(
        exchange.id, ExchangeStatus.AWAITING_USER, pending_question="Which city?"
    )
    task = orphaned_task(
        dialog,
        kind=TaskKind.ANSWER,
        title="which city?",
        exchange_id=exchange.id,
        input={"prompt": "which city?", "exchange_id": exchange.id},
    )
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    await manager.recover_interrupted()

    stored = await store.get(task.id)
    assert stored.status is TaskStatus.DONE
    assert stored.result == ""
    assert runner._processes == {}  # no replacement run was started
    settled = await exchanges.get(exchange.id)
    assert settled.status is ExchangeStatus.AWAITING_USER
    assert settled.pending_question == "Which city?"


async def test_stale_awaiting_exchange_triggers_a_nudge_on_a_new_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM([reply("answer to new")])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    stale = await exchanges.create(runner.dialog_id, "old question")
    await exchanges.set_status(
        stale.id, ExchangeStatus.AWAITING_USER, pending_question="Which city?"
    )
    await _backdate_exchange(session_factory, stale.id, NUDGE_STALE_SECONDS)

    await runner.submit("new question")
    nudge_content = NUDGE_TEMPLATE.format(title="old question", question="Which city?")
    events = await collect_until(queue, is_delivered(nudge_content))

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert any(item.message.content == nudge_content for item in finished)
    await manager.stop_all()


async def test_explicit_reply_joins_the_named_exchange_without_routing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    router = FakeRouter()
    llm = ScriptedLLM([blocking_call(), reply("second reply"), reply("third reply")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    live_exchange_id = runner.history()[0].exchange_id
    assert live_exchange_id is not None

    # a known, live exchange named explicitly: the deterministic shortcut, no LLM
    await runner.submit("clarification", reply_to_exchange_id=live_exchange_id)
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    assert runner.history()[1].exchange_id == live_exchange_id
    assert router.calls == []

    # a reply_to naming an exchange that is not live falls back to the router
    await runner.submit("stray reply", reply_to_exchange_id="not-a-live-exchange")
    await wait_for_message(runner, "stray reply")
    assert len(router.calls) == 1

    tool.release.set()
    await collect_completions(queue, TWO_PROCESSES)


def test_manager_satisfies_the_cron_waker_port() -> None:
    """The scheduler wakes dialogs through the manager directly (no adapter).

    Guards the structural match: if ConversationManager.wake ever drifts from
    the CronWaker protocol, this assignment stops type-checking and this
    runtime check fails — before the composition root finds out in prod.
    """
    waker_method = ConversationManager.wake
    port_method = CronWaker.wake
    waker_params = list(inspect.signature(waker_method).parameters)
    port_params = list(inspect.signature(port_method).parameters)

    assert waker_params == port_params


# --- 2026-07-28 race fixes: watermark, outbox identity, delivery, ask_user, sweep ---


class GatedAssembleCompactor:
    """ContextCompactor stub: full-history passthrough whose RETURN can be
    delayed on a chosen call, mirroring the production snapshot-then-await
    shape (`assemble` freezes `snapshot_len` and the message list the instant
    it is entered; only the caller sees the result later). A message
    appended to the live narrative during that delay must land past the
    frozen boundary and reach the run on a LATER sync instead of vanishing.
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.gate_on_call = 0
        self.calls = 0

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        self.calls += 1
        snapshot = list(history)  # frozen before any await, like the real compactor
        if self.calls == self.gate_on_call:
            self.entered.set()
            await self.gate.wait()
        return AssembledContext(
            messages=snapshot, tail_count=len(snapshot), snapshot_len=len(snapshot)
        )

    async def compacted_boundary(self, dialog_id: str) -> int:
        return 0

    async def compact_now(self, dialog: Dialog) -> bool:
        return True

    async def aclose(self, dialog_id: str) -> None:
        pass


async def test_message_appended_during_a_stalled_assemble_is_never_lost(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The watermark a process records comes from the compactor's snapshot,
    never from the live narrative length read after the assemble returns.

    Iteration 2's pull-model re-sync (`IterationStarted` -> `_sync_branch`)
    calls `_assemble_narrative` from inside the PUMP task, not the actor: a
    "setup" clarification submitted while the run's tool call was still
    blocking is what makes that sync call `assemble` at all (nothing pulls
    it otherwise); a SECOND message ("clarification") submitted while THAT
    call is stalled is invisible to the branch this run is about to answer
    with — but it must not be lost. It stays above the recorded watermark
    (taken from the compactor's frozen snapshot, not the live list), so once
    this run finishes without having seen it, the exchange reopens and a
    fresh run answers it. `_trim_narrative`'s drop is computed from the same
    frozen snapshot, so the message is never evicted from the in-memory
    narrative either.
    """
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), reply("first answer"), reply("final answer")])
    compactor = GatedAssembleCompactor()
    compactor.gate_on_call = 2  # iteration 2's sync, forced to run by "setup"
    router = FakeRouter()
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(compactor=compactor, router=router, store=store),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    exchange_id = runner.history()[0].exchange_id
    assert exchange_id is not None

    # picked up normally at the next sync: this is what makes iteration 2's
    # sync actually call assemble (nothing changed otherwise)
    router.decide_continue()
    await runner.submit("setup")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)

    tool.release.set()  # iteration 2 begins: its sync stalls inside assemble
    await asyncio.wait_for(compactor.entered.wait(), timeout=TIMEOUT_SECONDS)

    # the actor is free (the stall sits in the PUMP task): a second reply is
    # persisted and routed while the frozen snapshot is stuck mid-return
    await runner.submit("clarification")
    await wait_for_message(runner, "clarification")

    compactor.gate.set()  # release the stale (pre-clarification) snapshot
    await collect_completions(queue, TWO_PROCESSES)  # this run, then the reopened fresh run
    await wait_for_async_condition(lambda: _is_answered(exchanges, exchange_id))

    # never evicted from the in-memory narrative, and eventually reaches the model
    assert "clarification" in [m.content for m in runner.history()]
    assert any(
        CLARIFICATION_NOTE_TEMPLATE.format(content="clarification") in m.content
        for m in llm.requests[-1]
    )
    # the run that raced it never saw it: proof the loss would have been
    # silent without the fix (the message just did not exist for that call)
    assert not any("clarification" in m.content for m in llm.requests[1])


class GatedMarkDeliveredStore(InMemoryTaskStore):
    """TaskStore whose mark_delivered pauses until released.

    Freezes `_flush_deliveries` mid-loop: after it has peeked the head
    delivery and broadcast its events, but before it removes that delivery
    from the deque — the exact window in which an `ask_user` question can
    jump the queue via `appendleft`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def mark_delivered(self, task_id: str) -> None:
        self.entered.set()
        await self.gate.wait()
        await super().mark_delivered(task_id)


async def test_appendleft_during_a_stalled_flush_survives_identity_removal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`_flush_deliveries` removes the delivery it just sent by identity, not
    position: an `ask_user` question appendlefted while `mark_delivered`
    awaits must not be popped instead of the delivery that actually
    finished — both must reach the subscriber.
    """
    store = GatedMarkDeliveredStore()
    manager = make_manager(
        ScriptedLLM([]), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog, status=TaskStatus.DONE, result=TASK_RESULT)
    await store.add(task)

    runner.request_result_delivery(task.id)
    await asyncio.wait_for(store.entered.wait(), timeout=TIMEOUT_SECONDS)
    # the head delivery's events already reached the subscriber; only the
    # store write (mark_delivered) is stalled
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    notice_call = asyncio.create_task(runner._deliver_notice("the question", exchange_id="ex-fake"))
    await wait_for_condition(lambda: len(runner._pending_deliveries) == TWO_PENDING_DELIVERIES)

    store.gate.set()
    await notice_call
    events += await collect_until(queue, is_delivered("the question"))

    assert task.delivered_at is not None  # the original delivery was still stamped
    assert not runner._pending_deliveries  # nothing left stuck behind the question
    finished_contents = [
        e.payload.message.content for e in events if isinstance(e.payload, Finished)
    ]
    assert "the question" in finished_contents
    assert TASK_RESULT in finished_contents


async def test_answer_finished_with_zero_subscribers_waits_for_the_first_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An ANSWER run finishing with nobody watching must not be stamped
    delivered on the strength of the live stream alone: the terminal reached
    zero subscriber queues, so it goes through the outbox exactly like an
    unwatched RUN task's result and reaches the FIRST later subscriber.
    """
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([reply()])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    await runner.submit("hi")  # no subscriber attached at all

    async def has_a_task() -> bool:
        return len(await store.list(runner.dialog_id)) == 1

    await wait_for_async_condition(has_a_task)
    task = await single_task(store, runner.dialog_id)
    task = await wait_for_task_state(store, task.id, lambda t: t.status is TaskStatus.DONE)
    await asyncio.sleep(POLL_SECONDS * 5)  # give the actor a chance to misbehave

    assert (await store.get(task.id)).delivered_at is None
    assert runner._pending_deliveries != []

    queue = runner.subscribe()
    events = await collect_until(queue, is_delivered(REPLY))

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert finished[0].message.content == REPLY
    await wait_for_task_state(store, task.id, lambda t: t.delivered_at is not None)
    assert not runner._pending_deliveries


class AskThenStallLLM:
    """LLMClient stub: iteration 1 triggers the asking tool call; iteration 2
    streams partial text and then stalls until released, answering with
    `final_content` once resumed (empty models the asker ending in silence,
    having never synced a reply that arrived during the stall). Any later
    call (a reopened exchange's fresh run) answers immediately with
    `next_content`.
    """

    def __init__(self, final_content: str = "", next_content: str = REPLY) -> None:
        self.requests: list[list[ChatMessage]] = []
        self.release = asyncio.Event()
        # the asker reached its post-question turn: observed here rather than
        # through the stream, whose text the runner mutes once a run has asked
        self.stalled = asyncio.Event()
        self._final_content = final_content
        self._next_content = next_content

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        call_number = len(self.requests)
        if call_number == FIRST_CALL:
            yield StreamFinished(message=blocking_call())
        elif call_number == SECOND_CALL:
            yield LlmTextDelta(text=PARTIAL)
            self.stalled.set()
            await self.release.wait()
            yield StreamFinished(message=reply(self._final_content))
        else:
            yield StreamFinished(message=reply(self._next_content))


async def test_reply_while_the_asker_still_owns_the_exchange_spawns_no_second_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`ask_user` keeps the asking run's ownership: it delivers the question
    and parks the exchange AWAITING_USER, but the run itself stays alive
    (still generating its own next turn). A reply that arrives in that
    window must not spawn a second process for the same exchange — only
    once the asker actually ends (here: silently, having never seen the
    reply) does the exchange reopen for a fresh run.
    """
    tool = AskingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = AskThenStallLLM(final_content="", next_content="answered after all")
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    live = await exchanges.list_live(runner.dialog_id)
    assert [item.status for item in live] == [ExchangeStatus.AWAITING_USER]
    exchange_id = live[0].id
    assert len(runner._processes) == ONE_PROCESS  # the asker, still alive

    # the asker is mid-generation of its own next turn (still streaming)
    await asyncio.wait_for(llm.stalled.wait(), timeout=TIMEOUT_SECONDS)

    router.decide_continue()
    await runner.submit("Moscow")
    await wait_for_message(runner, "Moscow")
    # the reply landed while the asker still owns the exchange: no new run
    assert len(runner._processes) == ONE_PROCESS
    assert next(iter(runner._processes.values())).exchange_id == exchange_id

    llm.release.set()  # the asker finishes silently, never having synced "Moscow" in
    await wait_for_condition(lambda: not runner._processes)

    # only now does the exchange reopen, and a fresh run answers the reply
    await wait_for_async_condition(lambda: _is_answered(exchanges, exchange_id))
    events = await collect_until(queue, is_delivered("answered after all"))
    assert any(isinstance(e.payload, Finished) for e in events)
    assert [m.content for m in runner.history()] == [
        "what is the weather?",
        tool.question,
        "Moscow",
        "answered after all",
    ]


async def test_cancel_with_an_unseen_reply_settles_cancelled_not_reopened(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stop must not resurrect the exchange it just cancelled even when a
    reply arrived after the run's last sync: the cancel came after the
    message, so it covers it (`_settle_exchange`'s CANCELLED short-circuit
    skips the unseen-messages reopen).
    """
    llm = StallingLLM()
    router = FakeRouter()
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        llm, ToolRegistry(), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("work")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))
    exchange_id = runner.history()[0].exchange_id
    assert exchange_id is not None

    router.decide_continue()
    await runner.submit("more context")  # arrives after the run's last sync
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)

    await runner.cancel()
    llm.release.set()
    events = await collect_until(queue, is_completed)

    assert any(isinstance(e.payload, Cancelled) for e in events)
    await wait_for_async_condition(lambda: _is_cancelled(exchanges, exchange_id))
    assert runner._processes == {}
    await asyncio.sleep(POLL_SECONDS * 5)  # give a would-be resurrection a chance to appear
    assert runner._processes == {}
    still = await exchanges.get(exchange_id)
    assert still.status is ExchangeStatus.CANCELLED


async def test_cancel_closes_a_parked_awaiting_exchange_with_no_live_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stop button also closes an obligation that already finished
    asking and has nobody left running it (the run ended right after the
    ask) — otherwise the nudge would keep re-asking a question the user just
    said to drop.
    """
    tool = AskingTool()
    llm = ScriptedLLM([blocking_call(), reply("")])
    manager = make_manager(llm, blocking_registry(tool), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    await wait_for_condition(lambda: not runner._processes)  # the run ended, the ask is parked

    live = await exchanges.list_live(runner.dialog_id)
    assert [item.status for item in live] == [ExchangeStatus.AWAITING_USER]
    exchange_id = live[0].id

    await runner.cancel()

    await wait_for_async_condition(lambda: _is_cancelled(exchanges, exchange_id))


async def test_unowned_open_exchange_is_revived_when_a_process_slot_frees(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`_sweep_unowned_open` runs whenever a slot frees up: an OPEN exchange
    with no owner (a crash between exchange creation and owner assignment,
    or a limit window that never got a retry) waits quietly while every slot
    is busy, and gets a fresh run the moment one opens up.
    """
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    llm = ScriptedLLM([blocking_call(), reply("first done"), reply("stranded answered")])
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)

    # seed an OPEN, ownerless exchange with its message already in the
    # narrative (a crash between exchange creation and owner assignment)
    stranded = await exchanges.create(runner.dialog_id, "stranded question")
    stranded_message_id = await messages.append(
        runner.dialog_id, ChatMessage(role=MessageRole.USER, content="stranded question")
    )
    await messages.set_exchange(stranded_message_id, stranded.id)
    runner._narrative.append(
        ChatMessage(
            id=stranded_message_id,
            role=MessageRole.USER,
            content="stranded question",
            exchange_id=stranded.id,
        )
    )

    # the single slot is taken: nothing may spawn for the stranded exchange
    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    assert len(runner._processes) == ONE_PROCESS
    still_open = await exchanges.get(stranded.id)
    assert still_open.status is ExchangeStatus.OPEN
    assert runner._live_process_for(stranded.id) is None  # nothing spawned for it yet

    tool.release.set()
    events = await collect_until(queue, is_delivered("stranded answered"))

    assert any(isinstance(e.payload, Finished) for e in events)
    await wait_for_async_condition(lambda: _is_answered(exchanges, stranded.id))


async def test_recover_interrupted_revives_an_unowned_open_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The startup counterpart of the freed-slot sweep: an OPEN, ownerless
    exchange whose message is already stored (a crash before any run ever
    claimed it) gets a fresh run from `recover_interrupted`, the same way an
    orphaned task gets restarted.
    """
    llm = ScriptedLLM([reply("stranded answered")])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    dialog = await get_dialog(session_factory)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    exchange = await exchanges.create(dialog.id, "stranded question")  # OPEN, no owner
    message_id = await messages.append(
        dialog.id, ChatMessage(role=MessageRole.USER, content="stranded question")
    )
    await messages.set_exchange(message_id, exchange.id)

    await manager.recover_interrupted()

    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    events = await collect_until(queue, is_delivered("stranded answered"))

    assert any(isinstance(e.payload, Finished) for e in events)
    await wait_for_async_condition(lambda: _is_answered(exchanges, exchange.id))


async def test_stop_pressed_while_the_message_is_still_routing_cancels_its_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stop button covers a submit the actor has not finished routing yet."""
    router = GatedRouter()
    tool = BlockingTool()
    store = SqlAlchemyTaskStore(session_factory)
    manager = make_manager(
        ScriptedLLM([blocking_call(), reply("late answer")]),
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router, store=store),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    runner.subscribe()

    await runner.submit("first")  # no live exchange: routed without the router
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    router.gate_armed = True
    await runner.submit("second")  # the actor blocks inside the router
    await asyncio.wait_for(router.entered.wait(), timeout=TIMEOUT_SECONDS)
    await runner.cancel()  # stop pressed while "second" is mid-routing
    tool.release.set()
    router.release.set()

    async def second_run_cancelled() -> bool:
        tasks = await store.list(runner.dialog_id)
        return any("second" in task.title and task.status is TaskStatus.CANCELLED for task in tasks)

    await wait_for_async_condition(second_run_cancelled)
    await manager.stop_all()


def test_critical_events_are_never_evicted_by_a_later_critical_event() -> None:
    """A full queue admits a critical event only by dropping stream chatter."""

    def envelope(payload: LoopEvent, seq: int) -> ConversationEvent:
        return ConversationEvent(dialog_id="d", seq=seq, payload=payload)

    final = ChatMessage(role=MessageRole.ASSISTANT, content="a")
    queue: asyncio.Queue[ConversationEvent] = asyncio.Queue(maxsize=3)
    queue.put_nowait(envelope(Finished(message=final), 1))
    queue.put_nowait(envelope(TextDelta(text="chatter"), 2))
    queue.put_nowait(envelope(Failed(error="boom"), 3))
    incoming = envelope(Finished(message=replace(final, content="b")), 4)

    assert ConversationRunner._evict_and_put(queue, incoming) is True
    kept = [queue.get_nowait().seq for _ in range(3)]
    assert kept == [1, 3, 4]  # the delta is the victim; order preserved

    only_criticals: asyncio.Queue[ConversationEvent] = asyncio.Queue(maxsize=2)
    only_criticals.put_nowait(envelope(Finished(message=final), 1))
    only_criticals.put_nowait(envelope(Failed(error="x"), 2))
    assert ConversationRunner._evict_and_put(only_criticals, incoming) is False
    assert [only_criticals.get_nowait().seq for _ in range(2)] == [1, 2]


# --- Forwarded material (MessageKind.MATERIAL) and the collecting exchange -
#
# Forwarded material never opens an obligation on its own: it joins the
# dialog's single COLLECTING exchange (agent/runner.py:_collect_material),
# which only turns into a run either because a real (OWN) message adopts it
# or because CollectingSweeper nominates it once it falls quiet
# (agent/runner.py:_handle_promote). The actor's inbox is a single FIFO
# queue consumed one command at a time (`_run_actor`); several tests below
# exploit that to prove a "no-op" promotion already ran to completion,
# without a real sleep: a harmless follow-up submit is queued right after
# the command under test, and waiting for ITS effect to land guarantees the
# earlier command was fully handled first.


async def test_forwarding_material_collects_without_routing_or_starting_a_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router = FakeRouter()
    llm = ScriptedLLM([])  # a router call or a run would starve this and blow up
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(router=router))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await runner.submit(
        MATERIAL_TWO, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await runner.submit(
        MATERIAL_THREE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await wait_for_condition(lambda: len(runner.history()) == THREE_MATERIAL_MESSAGES)

    assert router.calls == []
    assert llm.requests == []
    exchange = await SqlAlchemyExchangeRepository(session_factory).find_collecting(runner.dialog_id)
    assert exchange is not None
    assert exchange.status is ExchangeStatus.COLLECTING
    # the title is pinned exactly: "Переслано от {origin}"
    assert exchange.title == MATERIAL_TITLE_TEMPLATE.format(origin=MATERIAL_ORIGIN)
    assert exchange.title == f"Переслано от {MATERIAL_ORIGIN}"
    forwarded = runner.history()
    assert [m.content for m in forwarded] == [MATERIAL_ONE, MATERIAL_TWO, MATERIAL_THREE]
    assert all(m.kind is MessageKind.MATERIAL for m in forwarded)
    assert all(m.exchange_id == exchange.id for m in forwarded)


async def test_forwarding_material_without_origin_titles_the_exchange_anonymous(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(ScriptedLLM([]), ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL)
    )  # no origin: anonymous forward
    await wait_for_condition(lambda: len(runner.history()) == 1)

    exchange = await SqlAlchemyExchangeRepository(session_factory).find_collecting(runner.dialog_id)
    assert exchange is not None
    # the title is pinned exactly: "Пересланные сообщения"
    assert exchange.title == MATERIAL_TITLE_ANONYMOUS
    assert exchange.title == "Пересланные сообщения"


async def test_promote_collected_starts_one_run_covering_all_collected_material(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM([reply(MATERIAL_REACTION)])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await runner.submit(
        MATERIAL_TWO, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await runner.submit(
        MATERIAL_THREE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await wait_for_condition(lambda: len(runner.history()) == THREE_MATERIAL_MESSAGES)
    exchange = await exchanges.find_collecting(runner.dialog_id)
    assert exchange is not None
    await _backdate_exchange(session_factory, exchange.id, seconds=MATERIAL_QUIET_SECONDS + 1)

    await runner.promote_collected(exchange.id)
    events = await collect_until(queue, is_delivered(MATERIAL_REACTION))

    assert len(llm.requests) == 1  # exactly one run for the whole batch
    branch_contents = [m.content for m in llm.requests[0]]
    assert MATERIAL_NOTE_TEMPLATE.format(content=MATERIAL_ONE) in branch_contents
    assert MATERIAL_NOTE_TEMPLATE.format(content=MATERIAL_TWO) in branch_contents
    # the last piece of material is the task; the date envelope wraps the tail
    assert branch_contents[-1].endswith(MATERIAL_TASK_NOTE_TEMPLATE.format(content=MATERIAL_THREE))
    assert any(isinstance(e.payload, Finished) for e in events)
    await wait_for_async_condition(lambda: _is_answered(exchanges, exchange.id))


async def test_promote_collected_is_a_noop_once_the_exchange_left_collecting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A question already adopted the collection between the sweep's nomination
    and the actor picking up the promotion: the re-check inside the actor
    must see that and do nothing (the anti-race guard `_handle_promote` exists for)."""
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(lambda: len(runner.history()) == 1)
    exchange = await exchanges.find_collecting(runner.dialog_id)
    assert exchange is not None
    await _backdate_exchange(session_factory, exchange.id, seconds=MATERIAL_QUIET_SECONDS + 1)
    await exchanges.set_status(exchange.id, ExchangeStatus.IN_PROGRESS)
    await add_exchange_task(session_factory, exchange.id, ADOPTING_TASK_ID)

    await runner.promote_collected(exchange.id)
    # FIFO proof: this submit's effect can only land after the promote above
    # was fully dispatched, since both go through the same single-consumer inbox
    await runner.submit(MATERIAL_TWO, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(lambda: len(runner.history()) == TWO_MESSAGES)

    settled = await exchanges.get(exchange.id)
    assert settled.status is ExchangeStatus.IN_PROGRESS
    assert llm.requests == []


async def test_promote_collected_is_a_noop_when_material_just_touched_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fresh material restarts the quiet clock: a promotion nominated before
    that arrival must find the window unmet and back off (the sweep's stale
    snapshot can lag the actor's live state by a full sweep interval)."""
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(lambda: len(runner.history()) == 1)
    exchange = await exchanges.find_collecting(runner.dialog_id)
    assert exchange is not None
    # not backdated: the touch from the submit above is still fresh

    await runner.promote_collected(exchange.id)
    await runner.submit(
        MATERIAL_TWO, source=MessageSource(kind=MessageKind.MATERIAL)
    )  # FIFO ordering proof
    await wait_for_condition(lambda: len(runner.history()) == TWO_MESSAGES)

    settled = await exchanges.get(exchange.id)
    assert settled.status is ExchangeStatus.COLLECTING
    assert llm.requests == []


async def test_own_message_adopts_the_sole_fresh_collection_without_asking_the_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ "Forward, then ask" is decided deterministically: the collection is the
    only live exchange and it is still fresh, so the message belongs to it.
    Asking an LLM here answered the question without the material (live,
    31.07) — the candidate line carries the forward's source, which says
    nothing about its subject."""
    router = FakeRouter()  # deliberately unprogrammed: it must not be consulted
    llm = ScriptedLLM([reply(MATERIAL_REACTION)])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(router=router))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await wait_for_condition(lambda: len(runner.history()) == 1)
    exchange = await exchanges.find_collecting(runner.dialog_id)
    assert exchange is not None

    await runner.submit("please summarize this")
    events = await collect_until(queue, is_delivered(MATERIAL_REACTION))

    assert router.calls == []  # no LLM stood between the forward and its question
    left_collecting = await exchanges.get(exchange.id)
    assert left_collecting.status is not ExchangeStatus.COLLECTING
    assert any(isinstance(e.payload, Finished) for e in events)
    await wait_for_async_condition(lambda: _is_answered(exchanges, exchange.id))


async def test_a_stale_collection_is_left_to_the_sweep_and_goes_to_the_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Past the quiet window the collection belongs to the promotion path.
    Adopting it here would race the sweep the actor serializes with, so the
    shortcut steps aside and the router decides — with the content in hand."""
    router = FakeRouter()
    llm = ScriptedLLM([reply(MATERIAL_REACTION)])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(router=router))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await wait_for_condition(lambda: len(runner.history()) == 1)
    exchange = await exchanges.find_collecting(runner.dialog_id)
    assert exchange is not None
    await _backdate_exchange(session_factory, exchange.id, seconds=MATERIAL_QUIET_SECONDS + 1)

    await runner.submit("unrelated question")
    await wait_for_condition(lambda: router.calls != [])


async def test_another_live_exchange_sends_the_message_to_the_router_with_the_preview(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With a second obligation in flight the message may be a reply to that
    one, so the shortcut does not fire. The router then needs what the
    collection holds: its title names the forward's source, never its
    subject."""
    tool = AskingTool()
    router = FakeRouter()
    llm = ScriptedLLM([blocking_call(), reply("")])
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router, store=SqlAlchemyTaskStore(session_factory)),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    # one obligation parked on the user, so the collection is not alone
    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    # after the run has ended, or the material joins it directly and no
    # collection ever stands beside the parked question (see the tail test)
    await wait_for_condition(lambda: runner._processes == {})
    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await wait_for_async_condition(lambda: _collection_exists(exchanges, runner.dialog_id))
    router.calls.clear()

    await runner.submit("what about the thing above?")
    await wait_for_condition(lambda: router.calls != [])

    candidates, _ = router.calls[0]
    collecting = [item for item in candidates if item.status is ExchangeStatus.COLLECTING]
    assert len(collecting) == 1
    assert collecting[0].preview is not None
    assert MATERIAL_ONE in collecting[0].preview
    # an ordinary exchange is described by its title; only material needs this
    others = [item for item in candidates if item.status is not ExchangeStatus.COLLECTING]
    assert [item.preview for item in others] == [None]


async def test_a_long_forward_keeps_its_tail_in_the_preview(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A forwarded picture leads with its attribution and the description of
    the image; the text the user forwarded trails it. Cutting the tail drops
    exactly what says what the post is about (live, 31.07: the router saw
    "a poster with a minimalist design" and nothing else), so an over-budget
    piece loses its middle instead."""
    tool = AskingTool()
    router = FakeRouter()
    llm = ScriptedLLM([blocking_call(), reply("")])
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router, store=SqlAlchemyTaskStore(session_factory)),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    # the ask is delivered while its run is still terminating; material sent
    # in that window joins the live exchange directly instead of opening the
    # collection this test is about. The SQL task store's extra awaits made
    # the race land there reliably — the in-memory store just hid it.
    await wait_for_condition(lambda: runner._processes == {})
    head = "[image] a poster with a minimalist design "
    tail = "the first travel MCP hackathon"
    await runner.submit(
        head + "x" * (MATERIAL_DIGEST_CHARS * 2) + tail,
        source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN),
    )
    await wait_for_async_condition(lambda: _collection_exists(exchanges, runner.dialog_id))
    router.calls.clear()

    await runner.submit("what about the thing above?")
    await wait_for_condition(lambda: router.calls != [])

    candidates, _ = router.calls[0]
    preview = next(item.preview for item in candidates if item.status is ExchangeStatus.COLLECTING)
    assert preview is not None
    assert len(preview) <= MATERIAL_DIGEST_CHARS
    assert preview.startswith(head)
    assert preview.endswith(tail)


async def test_continue_renames_the_exchange_to_what_it_is_now_about(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An exchange is named after the message that opened it, which stops
    describing it a few turns in. Routing renames it as it attaches, so the
    console, the nudge and the next routing decision see the current topic."""
    tool = AskingTool()
    router = FakeRouter()
    llm = ScriptedLLM([blocking_call(), reply(""), reply("+21 in Moscow")])
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router, store=SqlAlchemyTaskStore(session_factory)),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    live = await exchanges.list_live(runner.dialog_id)
    assert live[0].title == "what is the weather?"

    router.decide_continue(title="Погода в Москве")
    await runner.submit("Moscow")
    await collect_until(queue, is_delivered("+21 in Moscow"))

    await wait_for_async_condition(lambda: _has_title(exchanges, live[0].id, "Погода в Москве"))


async def test_continue_without_a_new_title_keeps_the_name_it_had(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`title=None` means "the name still fits" — never "clear it"."""
    tool = AskingTool()
    router = FakeRouter()
    llm = ScriptedLLM([blocking_call(), reply(""), reply("+21 in Moscow")])
    manager = make_manager(
        llm,
        blocking_registry(tool),
        session_factory,
        ManagerOptions(router=router, store=SqlAlchemyTaskStore(session_factory)),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("what is the weather?")
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    live = await exchanges.list_live(runner.dialog_id)

    router.decide_continue()  # no title offered
    await runner.submit("Moscow")
    await collect_until(queue, is_delivered("+21 in Moscow"))

    unchanged = await exchanges.get(live[0].id)
    assert unchanged.title == "what is the weather?"


async def test_cancel_cancels_a_collecting_exchange_and_blocks_later_promotion(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = ScriptedLLM([])
    manager = make_manager(llm, ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(lambda: len(runner.history()) == 1)
    exchange = await exchanges.find_collecting(runner.dialog_id)
    assert exchange is not None

    await runner.cancel()  # the stop button: "never mind", drop the pending material

    cancelled = await exchanges.get(exchange.id)
    assert cancelled.status is ExchangeStatus.CANCELLED

    await _backdate_exchange(session_factory, exchange.id, seconds=MATERIAL_QUIET_SECONDS + 1)
    await runner.promote_collected(exchange.id)
    await runner.submit(
        MATERIAL_TWO, source=MessageSource(kind=MessageKind.MATERIAL)
    )  # FIFO ordering proof
    await wait_for_condition(lambda: len(runner.history()) == TWO_MESSAGES)

    still_cancelled = await exchanges.get(exchange.id)
    assert still_cancelled.status is ExchangeStatus.CANCELLED
    assert llm.requests == []


async def test_material_never_triggers_the_process_limit_notice(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A blind spot the actor must not have: material bypasses routing/limit
    checks entirely (`_collect_material` never calls `_apply_route`), so a
    dialog sitting at its process limit still absorbs a forward silently."""
    tool = BlockingTool()
    llm = ScriptedLLM([blocking_call()])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(max_processes=ONE_PROCESS)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(lambda: len(runner.history()) == TWO_MESSAGES)

    # no canned notice was inserted: exactly the question and the forward
    assert [m.content for m in runner.history()] == ["start", MATERIAL_ONE]

    tool.release.set()
    await collect_until(queue, is_completed)


# --- Phase 3: promotion targeting another live exchange -------------------
#
# `_material_target` (agent/runner.py) asks the router ONCE, for the whole
# batch, whether a settled collection belongs to one of the dialog's other
# live exchanges. Only a CONTINUE into a known id counts as a target; the
# decision's `cancel_ids` are dropped entirely — forwarded text is untrusted,
# so a batch cannot cancel anything on its own say-so.


async def test_promotion_reparents_material_into_a_live_exchange_with_one_router_call(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A CONTINUE decision moves the whole batch into the named exchange
    instead of starting a run of its own, and the router is asked exactly
    once for the batch (not once per forward).

    The target is parked AWAITING_USER — live, but with no process still
    running it. That is deliberate: a run that is still live would have
    absorbed the material directly via `_material_home` (see
    `test_material_arriving_while_a_run_is_live_joins_its_exchange_directly`
    below), leaving no collection for a promotion to reparent at all.
    """
    tool = AskingTool()
    router = FakeRouter()
    llm = ScriptedLLM([blocking_call(), reply(""), reply("target final")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(TARGET_QUESTION)
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    await wait_for_condition(lambda: not runner._processes)  # asked, parked, run ended
    target_id = (await exchanges.list_live(runner.dialog_id))[0].id

    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await runner.submit(
        MATERIAL_TWO, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await runner.submit(
        MATERIAL_THREE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await wait_for_condition(
        lambda: (
            sum(1 for m in runner.history() if m.kind is MessageKind.MATERIAL)
            == THREE_MATERIAL_MESSAGES
        )
    )
    # no run was live: the material formed its own collection instead of
    # joining the parked exchange directly
    collection = await exchanges.find_collecting(runner.dialog_id)
    assert collection is not None
    await _backdate_exchange(session_factory, collection.id, seconds=MATERIAL_QUIET_SECONDS + 1)

    router.decide_continue()  # CONTINUE into exchanges[0]: the only other live exchange
    await runner.promote_collected(collection.id)
    await wait_for_async_condition(lambda: _is_cancelled(exchanges, collection.id))

    assert len(router.calls) == 1  # one call for the whole batch, not one per forward
    routed_exchanges, digest = router.calls[0]
    assert [item.id for item in routed_exchanges] == [target_id]  # the collection is excluded
    assert f"forwarded {THREE_MATERIAL_MESSAGES} message" in digest
    assert MATERIAL_ONE in digest
    assert MATERIAL_TWO in digest
    assert MATERIAL_THREE in digest

    reparented = [m for m in runner.history() if m.kind is MessageKind.MATERIAL]
    assert len(reparented) == THREE_MATERIAL_MESSAGES
    assert all(m.exchange_id == target_id for m in reparented)

    # the target had nobody running it: reparenting gives it a fresh run,
    # which answers and closes the obligation (IN_PROGRESS is too transient
    # to poll for reliably with a scripted, tool-free reply)
    await collect_until(queue, is_delivered("target final"))
    await wait_for_async_condition(lambda: _is_answered(exchanges, target_id))


async def test_router_new_decision_promotes_the_collection_as_its_own_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A NEW decision means the batch belongs to nobody else: it is promoted
    as its own exchange, with no reparenting, just like with no other live
    exchange at all.

    The other live exchange is parked AWAITING_USER (no process still
    running it) so the material forms its own collection instead of joining
    that exchange directly — see the reparenting test above for why.
    """
    tool = AskingTool()
    router = FakeRouter()  # default action is NEW
    llm = ScriptedLLM([blocking_call(), reply(""), reply(MATERIAL_REACTION)])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(TARGET_QUESTION)
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    await wait_for_condition(lambda: not runner._processes)  # asked, parked, run ended

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(
        lambda: sum(1 for m in runner.history() if m.kind is MessageKind.MATERIAL) == 1
    )
    collection = await exchanges.find_collecting(runner.dialog_id)
    assert collection is not None
    await _backdate_exchange(session_factory, collection.id, seconds=MATERIAL_QUIET_SECONDS + 1)

    await runner.promote_collected(collection.id)
    events = await collect_until(queue, is_delivered(MATERIAL_REACTION))

    assert len(router.calls) == 1  # consulted, but declined to claim the batch
    material_message = next(m for m in runner.history() if m.content == MATERIAL_ONE)
    assert material_message.exchange_id == collection.id  # never reparented
    await wait_for_async_condition(lambda: _is_answered(exchanges, collection.id))
    assert any(isinstance(e.payload, Finished) for e in events)


async def test_material_routing_ignores_cancel_ids_from_the_decision(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Injection guard: forwarded content is untrusted third-party text, so
    even a router decision that names `cancel_ids` for the batch must not
    cancel anything — only the user's own words can do that.

    The target is parked AWAITING_USER (no process still running it), same
    shape as the reparenting test above.
    """
    tool = AskingTool()
    router = FakeRouter()
    llm = ScriptedLLM([blocking_call(), reply(""), reply("target final")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(TARGET_QUESTION)
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    await wait_for_condition(lambda: not runner._processes)  # asked, parked, run ended
    target_id = (await exchanges.list_live(runner.dialog_id))[0].id

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(
        lambda: sum(1 for m in runner.history() if m.kind is MessageKind.MATERIAL) == 1
    )
    collection = await exchanges.find_collecting(runner.dialog_id)
    assert collection is not None
    await _backdate_exchange(session_factory, collection.id, seconds=MATERIAL_QUIET_SECONDS + 1)

    router.decide_continue()
    router.decide_cancel(target_id)  # a confused/hostile decision naming the live exchange
    await runner.promote_collected(collection.id)
    await wait_for_async_condition(lambda: _is_cancelled(exchanges, collection.id))

    # not cancelled by the material decision: it gets a fresh run instead,
    # which answers and closes the obligation normally
    await collect_until(queue, is_delivered("target final"))
    await wait_for_async_condition(lambda: _is_answered(exchanges, target_id))
    settled = await exchanges.get(target_id)
    assert settled.status is not ExchangeStatus.CANCELLED


async def test_command_decision_at_promotion_promotes_the_collection_on_its_own(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A COMMAND decision answers nothing about ownership: it reads as "no
    target", same as NEW — the collection is promoted on its own.

    The other live exchange is parked AWAITING_USER (no process still
    running it), same shape as the reparenting test above.
    """
    tool = AskingTool()
    router = FakeRouter()
    router.action = RouteAction.COMMAND
    llm = ScriptedLLM([blocking_call(), reply(""), reply(MATERIAL_REACTION)])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(TARGET_QUESTION)
    await asyncio.wait_for(tool.called.wait(), timeout=TIMEOUT_SECONDS)
    await collect_until(queue, is_delivered(tool.question))
    await wait_for_condition(lambda: not runner._processes)  # asked, parked, run ended

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(
        lambda: sum(1 for m in runner.history() if m.kind is MessageKind.MATERIAL) == 1
    )
    collection = await exchanges.find_collecting(runner.dialog_id)
    assert collection is not None
    await _backdate_exchange(session_factory, collection.id, seconds=MATERIAL_QUIET_SECONDS + 1)

    await runner.promote_collected(collection.id)
    events = await collect_until(queue, is_delivered(MATERIAL_REACTION))

    assert len(router.calls) == 1
    await wait_for_async_condition(lambda: _is_answered(exchanges, collection.id))
    assert any(isinstance(e.payload, Finished) for e in events)


async def test_material_landing_in_a_live_exchange_parks_it_instead_of_reopening(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No answer loops: material that lands in a live exchange after its run's
    last sync must not reopen it (that would be one requeue per forward — the
    very bug this model replaced). The run settles the exchange as COLLECTING,
    and the batch earns exactly one later reaction once it is promoted.

    Under the new rule the material joins the running exchange directly —
    `_material_home` finds the run still live and hands the forwards straight
    to it, so no separate collection is ever created here.
    """
    router = FakeRouter()
    llm = GatedLLM()
    manager = make_manager(llm, quick_registry(), session_factory, ManagerOptions(router=router))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit("start")
    # the run has synced once (the quick tool call) and is now stalled mid-
    # second-iteration, past its last sync — exactly like test_late_message_
    # reopens_the_exchange, but with material instead of a real reply
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))
    target_id = (await exchanges.list_live(runner.dialog_id))[0].id

    await runner.submit(MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL))
    await runner.submit(MATERIAL_TWO, source=MessageSource(kind=MessageKind.MATERIAL))
    await runner.submit(MATERIAL_THREE, source=MessageSource(kind=MessageKind.MATERIAL))
    await wait_for_condition(
        lambda: (
            sum(1 for m in runner.history() if m.kind is MessageKind.MATERIAL)
            == THREE_MATERIAL_MESSAGES
        )
    )
    # the run was still live: the material joined it directly, no collection
    assert await exchanges.find_collecting(runner.dialog_id) is None
    materials = [m for m in runner.history() if m.kind is MessageKind.MATERIAL]
    assert all(m.exchange_id == target_id for m in materials)

    llm.release.set()  # the run finishes, unaware of material it never synced
    events = await collect_until(queue, is_completed)

    # settling is a separate step the actor does after the ProcessCompleted
    # broadcast (via the queued _ProcessTerminated command): poll for it
    await wait_for_async_condition(lambda: _is_collecting(exchanges, target_id))
    first_completions = completions(events)
    assert len(first_completions) == 1  # exactly the run's own completion

    # the parked material later earns its own, single, later reaction — with
    # no other live exchange left, the promotion needs no router call at all
    await _backdate_exchange(session_factory, target_id, seconds=MATERIAL_QUIET_SECONDS + 1)
    await runner.promote_collected(target_id)
    later_events = await collect_until(queue, is_completed)

    total_completions = first_completions + completions(later_events)
    assert len(total_completions) == TWO_PROCESSES  # the run, plus exactly one later reaction
    assert router.calls == []  # nothing else live: `_material_target` skips the router


async def test_material_arriving_while_a_run_is_live_joins_its_exchange_directly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The production fix itself (`_material_home`, 2026-07-29): a forward
    that arrives while its exchange's own run is still live joins that
    exchange immediately — no separate collection, no promotion needed — and
    the running branch picks it up at its next sync. Before this change the
    forward went into the dialog's collecting exchange instead, invisible to
    the live run (`render_branch` dropped foreign live exchanges' own
    messages), and the run answered "I don't see any forwarded messages"
    (measured live)."""
    tool = BlockingTool()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    manager = make_manager(llm, blocking_registry(tool), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()
    exchanges = SqlAlchemyExchangeRepository(session_factory)

    await runner.submit(TARGET_QUESTION)
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    target_id = (await exchanges.list_live(runner.dialog_id))[0].id

    await runner.submit(
        MATERIAL_ONE, source=MessageSource(kind=MessageKind.MATERIAL, origin=MATERIAL_ORIGIN)
    )
    await wait_for_condition(lambda: len(runner.history()) == TWO_MESSAGES)

    # no collection was ever created: the forward joined the live question's
    # own exchange straight away
    assert await exchanges.find_collecting(runner.dialog_id) is None
    material_message = runner.history()[-1]
    assert material_message.kind is MessageKind.MATERIAL
    assert material_message.exchange_id == target_id

    tool.release.set()
    await collect_until(queue, is_completed)

    # the running branch re-synced and saw the forward at its next iteration
    second_request = llm.requests[1]
    assert any(
        MATERIAL_NOTE_TEMPLATE.format(content=MATERIAL_ONE) in m.content for m in second_request
    )


# --- 2026-07-29: looking again at an image the dialog already received --------


IMAGE_ANSWER = "в правом верхнем углу дата 12.2026"
IMAGE_QUESTION = "что в правом верхнем углу?"


class RecordingVision:
    """VisionClient stub recording what it was asked."""

    def __init__(self, answer: str = IMAGE_ANSWER) -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.images: list[bytes] = []

    async def look(self, images: tuple[ImageData, ...], prompt: str) -> str:
        self.prompts.append(prompt)
        self.images.extend(image.content for image in images)
        return self.answer


class RecordingResolver:
    """ImageResolver stub returning fixed bytes for a known ref."""

    def __init__(self) -> None:
        self.refs: list[str] = []

    async def fetch(self, ref: str) -> ImageData:
        self.refs.append(ref)
        return ImageData(content=b"jpeg-bytes", media_type="image/jpeg")


async def test_look_at_image_asks_about_the_newest_picture_of_the_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    vision, resolver = RecordingVision(), RecordingResolver()
    manager = make_manager(
        ScriptedLLM([]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(vision=vision, image_resolver=resolver),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    await runner.submit("старое фото", source=MessageSource(attachments=(OLD_IMAGE,)))
    await runner.submit("новое фото", source=MessageSource(attachments=(NEW_IMAGE,)))
    await wait_for_condition(lambda: len(runner.history()) == TWO_MESSAGES)

    answer = await runner.look_at_image(IMAGE_QUESTION)

    assert answer == IMAGE_ANSWER
    assert resolver.refs == [NEW_IMAGE.ref]  # the newest one, not the first
    assert vision.prompts == [IMAGE_QUESTION]
    assert vision.images == [b"jpeg-bytes"]
    await manager.stop_all()


async def test_look_at_image_sees_every_picture_of_the_newest_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An album is one message with N pictures (three pages of one menu):
    looking again must show the strong tier all of them in one call, or it
    answers "there is no chicken on this menu" from page one — the very
    blindness the tool exists to cure."""
    vision, resolver = RecordingVision(), RecordingResolver()
    manager = make_manager(
        ScriptedLLM([]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(vision=vision, image_resolver=resolver),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    await runner.submit("старое фото", source=MessageSource(attachments=(OLD_IMAGE,)))
    pages = (NEW_IMAGE, Attachment(kind=AttachmentKind.IMAGE, ref="tg:page-2"))
    await runner.submit("меню в картинках", source=MessageSource(attachments=pages))
    await wait_for_condition(lambda: len(runner.history()) == TWO_MESSAGES)

    await runner.look_at_image(IMAGE_QUESTION)

    assert resolver.refs == [page.ref for page in pages]  # both pages, the older one left out
    assert vision.images == [b"jpeg-bytes", b"jpeg-bytes"]  # one call, both pictures in it
    assert vision.prompts == [IMAGE_QUESTION]
    await manager.stop_all()


async def test_look_at_image_without_any_picture_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(
        ScriptedLLM([]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(vision=RecordingVision(), image_resolver=RecordingResolver()),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    with pytest.raises(VisionUnavailableError):
        await runner.look_at_image(IMAGE_QUESTION)
    await manager.stop_all()


async def test_look_at_image_without_vision_configured_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole capability is optional: no model, no resolver, no tool."""
    manager = make_manager(ScriptedLLM([]), ToolRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    await runner.submit("фото", source=MessageSource(attachments=(NEW_IMAGE,)))
    await wait_for_condition(lambda: len(runner.history()) == 1)

    with pytest.raises(VisionUnavailableError):
        await runner.look_at_image(IMAGE_QUESTION)
    await manager.stop_all()


class _MarkDoneUnavailable(SqlAlchemyTaskStore):
    """A task store whose `mark_done` is down (the finalize-unit rollback test)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.failures = 0

    async def mark_done(self, task_id: str, result: str, *, delivered: bool = False) -> Task:
        self.failures += 1
        raise RuntimeError("mark_done unavailable")


async def test_a_failed_finalize_takes_the_persisted_answer_with_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The answer and the task's terminal state land together or not at all.

    With `mark_done` down, committing the answer alone would leave an
    answered narrative under a task that still claims to be running — and
    recovery would then answer the question a second time. The unit of work
    around the finalize rolls the answer back instead: the store ends in the
    consistent "this run never finished" state.
    """
    store = _MarkDoneUnavailable(session_factory)
    manager = make_manager(
        ScriptedLLM([reply()]), ToolRegistry(), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    await collect_until(queue, is_completed)

    assert store.failures == 1
    task = await single_task(store, runner.dialog_id)
    assert task.finished_at is None  # never marked done: the write failed
    dialog = await get_dialog(session_factory)
    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(MessageRow).where(MessageRow.dialog_id == dialog.id).order_by(MessageRow.seq)
            )
        ).all()
    # the question alone survives; the answer rolled back with the unit
    assert [row.role for row in rows] == [MessageRole.USER.value]
    await manager.stop_all()
