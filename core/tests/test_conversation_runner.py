"""Tests for ConversationRunner processes and ConversationManager."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    ProcessCompleted,
    ProcessSuspended,
    TextDelta,
    ToolCallRequested,
)
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import (
    MessageRouter,
    ProcessInfo,
    RouteAction,
    RouteDecision,
    RouteOp,
)
from octoforge_core.agent.runner import (
    BACKGROUND_TASK_PROMPT,
    RESTART_LIMIT_ERROR,
    SUBSCRIBER_QUEUE_SIZE,
    ConversationEvent,
    ConversationManager,
    RunnerConfig,
    TaskOutcomeListener,
)
from octoforge_core.context.api import INTERRUPTED_NOTE, AssembledContext, ContextCompactor
from octoforge_core.context.compactor import NoopContextCompactor
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.models import MessageRow
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, Dialog, MessageRole, ToolCall
from octoforge_core.llm.errors import ContextOverflowError
from octoforge_core.llm.events import StreamEvent, StreamFinished, ToolCallReady
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion, Usage
from octoforge_core.ports import LLMClient
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.spawner import TaskDeleteOutcome
from octoforge_core.tasks.store import InMemoryTaskStore, TaskStore
from octoforge_core.tasks.tools import TaskCreateTool, TaskDeleteTool
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.registry import ToolRegistry

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
USER_ID = "user-1"
CHANNEL = "web"
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
FIRST_CALL = 1
SECOND_CALL = 2
EXPECTED_LLM_CALLS = 3
MESSAGES_AFTER_REFUSAL = 3
MESSAGES_AFTER_INJECT = 2


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


class FakeRouter:
    """MessageRouter stub with a programmable decision handler."""

    def __init__(self) -> None:
        self.handler: Callable[[tuple[ProcessInfo, ...], str], RouteDecision] = (
            lambda processes, message: RouteDecision()
        )
        self.calls: list[tuple[tuple[ProcessInfo, ...], str]] = []

    async def route(
        self,
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> RouteDecision:
        self.calls.append((processes, message))
        return self.handler(processes, message)

    def decide(self, *ops: RouteOp) -> None:
        self.handler = lambda processes, message: RouteDecision(ops=ops)


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
        return AssembledContext(messages=list(history), tail_count=len(history))

    async def compacted_boundary(self, dialog_id: str) -> int:
        return 0

    async def compact_now(self, dialog: Dialog) -> bool:
        self.compact_calls += 1
        if self._compact_error is not None:
            raise self._compact_error
        return self._compact_result

    async def aclose(self, dialog_id: str) -> None:
        self.closed.append(dialog_id)


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
    )
    return ConversationManager(
        config=config,
        dialogs=DialogRepository(session_factory),
        messages=MessageRepository(session_factory),
        tasks=resolved.store if resolved.store is not None else InMemoryTaskStore(),
    )


async def get_dialog(session_factory: async_sessionmaker[AsyncSession]) -> Dialog:
    return await DialogRepository(session_factory).get_or_create(USER_ID, CHANNEL)


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
    store = InMemoryTaskStore()
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
    assert task.input == {"prompt": "hi", "source_message_id": source_message.id}
    assert task.status is TaskStatus.DONE
    # the foreground stream was the delivery: the actor stamps it asynchronously
    await wait_for_condition(lambda: task.delivered_at is not None)
    assert runner._pending_deliveries == []  # nothing left in the outbox
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="hi"),
        replace(reply(), task_id=task.id),
    ]
    dialog = await get_dialog(session_factory)
    assert await MessageRepository(session_factory).list(dialog.id) == runner.history()


async def test_finished_usage_is_persisted_on_the_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    usage = Usage(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS)
    store = InMemoryTaskStore()
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
    stored = await MessageRepository(session_factory).list(dialog.id)
    assert stored[0] == ChatMessage(role=MessageRole.USER, content="hi")


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

    assert len(router.calls) == RETRIED_CALLS  # the duplicate never reached the router
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


async def test_suspended_process_final_is_delivered_whole_after_the_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    store = InMemoryTaskStore()
    llm = ScriptedLLM([blocking_call(), reply("second final"), reply("first final")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")
    events = await collect_completions(queue, 1)

    suspended = [e.payload for e in events if isinstance(e.payload, ProcessSuspended)]
    assert [(item.title) for item in suspended] == ["first"]
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.message.content for item in finished] == ["second final"]

    tool.release.set()
    events += await collect_until(queue, is_delivered("first final"))

    # the suspended process finished in the background: its final arrives whole
    # (TextDelta + Finished) after the foreground's events
    deltas = [e.payload.text for e in events if isinstance(e.payload, TextDelta)]
    assert deltas == ["second final", "first final"]
    done = completions(events)
    assert {item.title for item in done} == {"first", "second"}
    assert all(item.status == TaskStatus.DONE.value for item in done)
    tasks = await store.list(runner.dialog_id)
    assert {task.title for task in tasks} == {"first", "second"}
    assert all(task.status is TaskStatus.DONE for task in tasks)
    assert all(task.delivered_at is not None for task in tasks)
    assert runner._pending_deliveries == []
    history = runner.history()
    assert [m.content for m in history] == ["first", "second", "second final", "first final"]
    task_ids = {task.id for task in tasks}
    assert all(m.task_id in task_ids for m in history if m.role is MessageRole.ASSISTANT)


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
    router.decide(RouteOp(action=RouteAction.INJECT))
    await runner.submit("extra context")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    assert len(llm.requests) == 1  # INJECT is a no-op: no new run, no push channel
    tool.release.set()
    await collect_until(queue, is_completed)

    # the running process re-synced its branch: the message arrived at iteration 2
    second_request = llm.requests[1]
    contents = [m.content for m in second_request]
    assert any(m.role is MessageRole.USER and "extra context" in m.content for m in second_request)
    assert contents.index("start") < next(
        i for i, content in enumerate(contents) if "extra context" in content
    )
    # a message the process did sync is not re-routed at finalize: one process only
    assert len(llm.requests) == RETRIED_CALLS
    assert [m.content for m in runner.history()] == ["start", "extra context", "after"]


async def test_unseen_message_is_requeued_at_finalize(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = GatedLLM()
    router = FakeRouter()
    store = InMemoryTaskStore()
    manager = make_manager(
        llm, quick_registry(), session_factory, ManagerOptions(router=router, store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))

    # the message lands after the process's last sync: the run finishes without it
    router.decide(RouteOp(action=RouteAction.INJECT))
    await runner.submit("extra context")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    llm.release.set()
    await collect_completions(queue, 2)

    # the watermark re-routes it: a new answer task picks it up
    assert len(llm.requests) == EXPECTED_LLM_CALLS
    third_request = llm.requests[2]
    assert any(m.role is MessageRole.USER and m.content == "extra context" for m in third_request)
    assert [m.content for m in runner.history()] == [
        "start",
        "extra context",
        "first final",
        "after",
    ]
    # the re-routed answer task links to its source message (the id rides the
    # narrative copy captured at submit)
    tasks = {task.title: task for task in await store.list(runner.dialog_id)}
    rerouted = tasks["extra context"]
    source_message = runner.history()[1]
    assert source_message.id is not None
    assert rerouted.input["source_message_id"] == source_message.id


async def test_bring_back_starts_a_new_answer_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    store = InMemoryTaskStore()
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
    events = await collect_completions(queue, 1)
    await runner.submit("bring back the first one")
    events += await collect_completions(queue, 1)

    # there is no promote route: a "bring back" request is a plain START_NEW —
    # the fresh foreground process sees the whole narrative, including the
    # suspended task's question and the other answer
    suspended = [e.payload.title for e in events if isinstance(e.payload, ProcessSuspended)]
    assert suspended == ["first"]  # "second" already finished when "bring back" arrived
    bring_back_request = llm.requests[2]
    assert [m.content for m in bring_back_request[1:-1]] == ["first", "second", "second final"]
    assert "bring back the first one" in bring_back_request[-1].content

    tool.release.set()
    events += await collect_until(queue, is_delivered("first final"))

    done = completions(events)
    assert {item.title for item in done} == {"first", "second", "bring back the first one"}
    assert all(item.status == TaskStatus.DONE.value for item in done)
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
    store = InMemoryTaskStore()
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

    def cancel_all(processes: tuple[ProcessInfo, ...], message: str) -> RouteDecision:
        return RouteDecision(
            ops=tuple(
                RouteOp(action=RouteAction.CANCEL, target_id=process.id) for process in processes
            )
        )

    router.handler = cancel_all
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


async def test_cancel_api_cancels_only_the_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    store = InMemoryTaskStore()
    llm = ScriptedLLM([blocking_call(), blocking_call(), reply("first final")])
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await runner.submit("second")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))

    await runner.cancel()
    tool.release.set()
    events = await collect_completions(queue, 2)
    events += await collect_until(queue, is_delivered("first final"))

    by_title = {item.title: item.status for item in completions(events)}
    assert by_title == {"second": TaskStatus.CANCELLED.value, "first": TaskStatus.DONE.value}
    assert any(isinstance(e.payload, Cancelled) for e in events)
    # the cancelled process delivers nothing; the background one comes via the outbox
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.message.content for item in finished] == ["first final"]
    tasks = {task.title: task for task in await store.list(runner.dialog_id)}
    assert tasks["second"].status is TaskStatus.CANCELLED  # the row is kept
    assert tasks["first"].status is TaskStatus.DONE
    assert [m.content for m in runner.history()] == ["first", "second", "first final"]


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
    tool.release.set()
    events = await collect_completions(queue, 1)
    events += await collect_until(queue, is_delivered(runner.history()[2].content))

    assert len(completions(events)) == 1  # no new process was started for the question
    notice = runner.history()[2]
    assert notice.role is MessageRole.ASSISTANT
    assert "process limit (1)" in notice.content
    assert "start" in notice.content
    # the queued notice is delivered once the foreground is free
    deltas = [e.payload.text for e in events if isinstance(e.payload, TextDelta)]
    assert deltas == ["after", notice.content]
    assert len(llm.requests) == RETRIED_CALLS  # still no extra run


async def test_process_limit_notice_flushes_immediately_when_foreground_is_free(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    llm.gate_background = True
    store = InMemoryTaskStore()
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
    assert runner._pending_deliveries == []


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
    store = InMemoryTaskStore()
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
    assert runner._pending_deliveries == []

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
    store = InMemoryTaskStore()
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
    store = InMemoryTaskStore()
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


async def test_background_delivery_waits_for_a_busy_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tool = BlockingTool()
    llm = BranchLLM(main=[blocking_call(), reply("after")], background=[reply(TASK_RESULT)])
    store = InMemoryTaskStore()
    manager = make_manager(
        llm, blocking_registry(tool), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(tool.started.wait(), timeout=TIMEOUT_SECONDS)

    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    (task,) = [t for t in await store.list(runner.dialog_id) if t.kind is TaskKind.RUN]
    await wait_for_condition(lambda: task.status is TaskStatus.DONE)
    # the foreground is busy: the finished result waits in the outbox, unstamped
    assert task.delivered_at is None
    assert len(runner._pending_deliveries) == 1

    tool.release.set()
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    # delivered whole, after the foreground's own stream events
    deltas = [e.payload.text for e in events if isinstance(e.payload, TextDelta)]
    assert deltas == ["after", TASK_RESULT]
    assert task.delivered_at is not None
    assert runner._pending_deliveries == []
    # the foreground pulled the background final into its next iteration
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
    assert runner._pending_deliveries == []
    # the retry re-sends the events: the transport may see a duplicate
    result_deltas = [e.payload.text for e in events if isinstance(e.payload, TextDelta)].count(
        TASK_RESULT
    )
    assert result_deltas == RETRIED_CALLS


async def test_interrupted_turn_is_salvaged_into_the_narrative(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = StallingLLM()
    store = InMemoryTaskStore()
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
    assert await MessageRepository(session_factory).list(dialog.id) == history


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
    assert await MessageRepository(session_factory).list(dialog.id) == history


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


async def test_wake_runs_cron_tagged_background_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[], background=[reply(TASK_RESULT)])
    store = InMemoryTaskStore()
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    delivered = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    assert delivered is True
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
    assert runner._pending_deliveries == []
    assert llm.main_requests == []  # no report run
    background_request = llm.background_requests[0]
    assert background_request[0].content == BACKGROUND_TASK_PROMPT  # no volatile date
    assert background_request[1].role is MessageRole.USER
    assert background_request[1].content.endswith(CRON_PROMPT)  # date envelope + prompt


async def test_wake_delivers_a_failed_event_for_a_failed_cron_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = FailingTaskLLM()
    store = InMemoryTaskStore()
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    delivered = await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    assert delivered is True
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
    store = InMemoryTaskStore()
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
    assert all(task.delivered_at is not None for task in tasks)
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
    store = InMemoryTaskStore()
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

    tool.release.set()
    events = await collect_until(queue, is_delivered(note.content))
    deltas = [e.payload.text for e in events if isinstance(e.payload, TextDelta)]
    assert deltas == ["after", note.content]  # flushed once the foreground is free
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
    store = InMemoryTaskStore()
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
    router = FakeRouter()
    seen = {"calls": 0}

    def handler(processes: tuple[ProcessInfo, ...], message: str) -> RouteDecision:
        seen["calls"] += 1
        if seen["calls"] == FIRST_CALL:
            raise RuntimeError("router boom")
        return RouteDecision()

    router.handler = handler
    manager = make_manager(
        ScriptedLLM([reply()]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(router=router),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")  # router raises: the actor must log and stay alive
    await runner.submit("second")  # processed normally, proving the actor survived

    events = await collect_until(queue, is_completed)
    done = completions(events)
    assert [item.status for item in done] == [TaskStatus.DONE.value]
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
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
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
    manager = make_manager(
        ScriptedLLM([reply()]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(router=router),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    await runner.submit("hi")
    await asyncio.wait_for(router.entered.wait(), timeout=TIMEOUT_SECONDS)
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
        return AssembledContext(messages=list(history), tail_count=len(history))

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


def orphaned_task(dialog: Dialog, cron_job_id: str | None = None, **overrides: object) -> Task:
    """Build a RUN task as a previous instance left it: stored and running."""
    task_input: dict[str, Any] = {"title": TASK_TITLE, "prompt": TASK_PROMPT}
    if cron_job_id is not None:
        task_input["cron_job_id"] = cron_job_id
    task = Task(
        dialog_id=dialog.id,
        user_id=dialog.user_id,
        channel=dialog.channel,
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
    store = InMemoryTaskStore()
    llm = ScriptedLLM([reply(TASK_RESULT)])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    task = orphaned_task(dialog)
    await store.add(task)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.recover_interrupted()
    events = await collect_until(queue, is_delivered(TASK_RESULT))

    assert task.status is TaskStatus.DONE  # the restarted process finished it
    assert task.delivered_at is not None
    # queue-mode: the process never became the foreground — the completion
    # marker precedes the delivered events (a foreground would stream first)
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [(TASK_TITLE, TaskStatus.DONE.value)]
    assert isinstance(events[-1].payload, Finished)
    assert runner._foreground_id is None
    # the RUN restart branch is self-contained: background prompt + task prompt
    request = llm.requests[0]
    assert request[0].content == BACKGROUND_TASK_PROMPT
    assert request[1].role is MessageRole.USER
    assert request[1].content.endswith(TASK_PROMPT)


async def test_recover_interrupted_restarts_orphaned_answer_tasks(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
    llm = ScriptedLLM([reply("re-answer")])
    manager = make_manager(llm, ToolRegistry(), session_factory, ManagerOptions(store=store))
    dialog = await get_dialog(session_factory)
    # the question lives in the narrative, persisted before the crash
    await MessageRepository(session_factory).append(
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

    assert task.status is TaskStatus.DONE
    # the ANSWER restart branch re-attaches to the narrative: system + snapshot
    request = llm.requests[0]
    assert request[0] == ChatMessage(role=MessageRole.SYSTEM, content=PROMPT)
    assert request[1].role is MessageRole.USER
    assert request[1].content.endswith("unanswered question")


async def test_recover_interrupted_reports_cron_outcome_after_the_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
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


async def test_recover_interrupted_redelivers_undelivered_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
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
    assert task.delivered_at is not None
    assert llm.requests == []  # redelivery needs no LLM run


async def test_recover_interrupted_redelivers_undelivered_failures(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
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
    assert task.delivered_at is not None
    assert runner.history() == []


async def test_recover_interrupted_skips_already_delivered_results(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
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
    store = InMemoryTaskStore()
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
    await wait_for_condition(lambda: task.delivered_at is not None)
    assert llm.requests == []  # redelivery needs no LLM run


async def test_cron_result_of_an_unwatched_dialog_waits_for_a_subscriber(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A firing whose dialog has no transport attached keeps its result queued."""
    store = InMemoryTaskStore()
    manager = make_manager(
        BranchLLM(main=[], background=[reply(TASK_RESULT)]),
        ToolRegistry(),
        session_factory,
        ManagerOptions(store=store),
    )

    assert await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID) is True
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    task = await single_task(store, runner.dialog_id)
    await wait_for_condition(lambda: task.status is TaskStatus.DONE)
    await asyncio.sleep(POLL_SECONDS * 5)

    assert task.delivered_at is None
    assert runner._pending_deliveries != []  # the outbox holds it

    queue = runner.subscribe()
    await collect_until(queue, is_delivered(TASK_RESULT))

    await wait_for_condition(lambda: task.delivered_at is not None)
    assert runner._pending_deliveries == []


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
    store = InMemoryTaskStore()
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

    assert task.status is TaskStatus.FAILED
    assert task.error == RESTART_LIMIT_ERROR

    tool.release.set()
    events = await collect_until(queue, lambda e: isinstance(e.payload, Failed))

    failed = [e.payload for e in events if isinstance(e.payload, Failed)]
    assert any(item.error == RESTART_LIMIT_ERROR for item in failed)
    assert task.delivered_at is not None


async def test_request_result_delivery_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
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

    assert task.delivered_at is not None
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
        processes: tuple[ProcessInfo, ...],
        message: str,
        max_processes: int,
    ) -> RouteDecision:
        if processes:  # first message routes instantly (empty snapshot skips the LLM anyway)
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


class SlowHistoryRepository(MessageRepository):
    """MessageRepository whose history load blocks until released."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)
        self.release = asyncio.Event()
        self.loads = 0

    async def list_after(self, dialog_id: str, after_seq: int) -> list[ChatMessage]:
        self.loads += 1
        if self.loads == 1:  # only the FIRST contact is slow
            await self.release.wait()
        return await super().list_after(dialog_id, after_seq)


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
        return AssembledContext(messages=kept, tail_count=len(kept))

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
    """A runner never loads compacted history into memory (S3)."""
    dialogs = DialogRepository(session_factory)
    messages = MessageRepository(session_factory)
    dialog = await dialogs.get_or_create(USER_ID, CHANNEL)
    for content in ("old-1", "old-2", "hot-3"):
        await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content=content))
    manager = make_manager(
        ScriptedLLM([]),
        quick_registry(),
        session_factory,
        ManagerOptions(compactor=TailCompactor(tail=10, boundary=2)),
    )

    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)

    assert [message.content for message in runner.history()] == ["hot-3"]
    await manager.stop_all()


async def test_trim_narrative_remaps_watermarks_and_prunes_coverage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """White-box: trimming shifts process watermarks and drops stale covered ids."""
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
    assert first_message.id in runner._covered_ids

    runner._trim_narrative(0)  # everything compacted away

    assert runner.history() == []
    assert process.watermark == 0 and watermark_before > 0
    assert first_message.id not in runner._covered_ids

    tool.release.set()
    await manager.stop_all()
