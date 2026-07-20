"""Tests for ConversationRunner processes and ConversationManager."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    ProcessCompleted,
    ProcessResumed,
    ProcessSuspended,
    TextDelta,
    ToolCallRequested,
)
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import (
    ProcessInfo,
    RouteAction,
    RouteDecision,
    RouteOp,
)
from octoforge_core.agent.runner import (
    BACKGROUND_TASK_PROMPT,
    INTERRUPTED_NOTE,
    REPORT_NUDGE,
    ConversationEvent,
    ConversationManager,
    RunnerConfig,
)
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, Dialog, MessageRole, ToolCall
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.ports import LLMClient, TaskStore
from octoforge_core.skills.base import SkillContext, SkillOrigin, SkillSpec
from octoforge_core.skills.basic.task_spawn import TaskSpawnSkill
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.models import Task, TaskStatus
from octoforge_core.tasks.store import InMemoryTaskStore

PROMPT = "test system prompt"
REPLY = "hello"
PARTIAL = "partial"
BLOCKING_SKILL = "blocking"
TASK_SPAWN_CALL = "task_spawn"
CALL_ID = "call-1"
TASK_RESULT = "42"
TASK_TITLE = "research"
TASK_PROMPT = "solve 2+2"
UNBLOCKED_OUTPUT = "unblocked"
TIMEOUT_SECONDS = 2.0
POLL_SECONDS = 0.01
MAX_ITERATIONS = 3
MAX_PROCESSES = 5
ONE_PROCESS = 1
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
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        self.requests.append(list(messages))
        return self._replies.pop(0)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        reply = self._replies.pop(0)
        if reply.content:
            yield LlmTextDelta(text=reply.content)
        yield StreamFinished(message=reply)


class GatedLLM:
    """LLMClient stub pausing its second reply until released."""

    def __init__(self) -> None:
        self.requests: list[list[ChatMessage]] = []
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
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
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
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


class StallingLLM:
    """LLMClient stub emitting partial text and stalling until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        raise NotImplementedError

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=PARTIAL)
        await self.release.wait()
        yield StreamFinished(message=reply("full"))


class BlockingSkill:
    """Skill stub holding the run open until released."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=BLOCKING_SKILL, description="blocks", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        self.started.set()
        await self.release.wait()
        return UNBLOCKED_OUTPUT


class QuickSkill:
    """Skill stub completing immediately."""

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=BLOCKING_SKILL, description="quick", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        return "ok"


def blocking_call() -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=CALL_ID, name=BLOCKING_SKILL, arguments={}),),
    )


def task_spawn_call() -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(
            ToolCall(
                id=CALL_ID,
                name=TASK_SPAWN_CALL,
                arguments={"title": TASK_TITLE, "prompt": TASK_PROMPT},
            ),
        ),
    )


def reply(content: str = REPLY) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


@dataclass(frozen=True, slots=True)
class ManagerOptions:
    """Optional knobs for building a test ConversationManager."""

    router: FakeRouter | None = None
    store: TaskStore | None = None
    max_processes: int = MAX_PROCESSES


def make_manager(
    llm: LLMClient,
    registry: SkillRegistry,
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
    )
    return ConversationManager(
        config=config,
        dialogs=DialogRepository(session_factory),
        messages=MessageRepository(session_factory),
        tasks=resolved.store if resolved.store is not None else InMemoryTaskStore(),
    )


async def get_dialog(session_factory: async_sessionmaker[AsyncSession]) -> Dialog:
    return await DialogRepository(session_factory).get_or_create(USER_ID, CHANNEL)


def blocking_registry(skill: BlockingSkill) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(skill, SkillOrigin.BASIC)
    return registry


def quick_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(QuickSkill(), SkillOrigin.BASIC)
    return registry


def is_terminal(event: ConversationEvent) -> bool:
    return isinstance(event.payload, (Finished, Failed, Cancelled))


def is_completed(event: ConversationEvent) -> bool:
    return isinstance(event.payload, ProcessCompleted)


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


async def test_submit_streams_events_and_updates_narrative(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = make_manager(ScriptedLLM([reply()]), SkillRegistry(), session_factory)
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
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="hi"),
        reply(),
    ]
    dialog = await get_dialog(session_factory)
    assert await MessageRepository(session_factory).list(dialog.id) == runner.history()


async def test_new_question_suspends_foreground_and_starts_new_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([blocking_call(), reply("second final"), reply("first final")])
    manager = make_manager(llm, blocking_registry(skill), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")
    events = await collect_completions(queue, 1)

    suspended = [e.payload for e in events if isinstance(e.payload, ProcessSuspended)]
    assert [(item.title) for item in suspended] == ["first"]
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert len(finished) == 1
    assert finished[0].message.content == "second final"

    skill.release.set()
    events += await collect_completions(queue, 1)

    done = completions(events)
    assert {item.title for item in done} == {"first", "second"}
    assert all(item.status == TaskStatus.DONE.value for item in done)
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="first"),
        ChatMessage(role=MessageRole.USER, content="second"),
        reply("second final"),
        reply("first final"),
    ]


async def test_inject_steers_the_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(skill), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    router.decide(RouteOp(action=RouteAction.INJECT))
    await runner.submit("extra context")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    await asyncio.sleep(0)
    skill.release.set()
    await collect_until(queue, is_completed)

    second_request = llm.requests[1]
    assert any(m.role is MessageRole.USER and m.content == "extra context" for m in second_request)
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="start"),
        ChatMessage(role=MessageRole.USER, content="extra context"),
        reply("after"),
    ]


async def test_promote_brings_background_process_to_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = GatedLLM()
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(skill), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("second")
    events = await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))

    suspended = [e.payload for e in events if isinstance(e.payload, ProcessSuspended)]
    assert [(item.title) for item in suspended] == ["first"]
    first_id = suspended[0].process_id

    router.decide(RouteOp(action=RouteAction.PROMOTE, target_id=first_id))
    await runner.submit("bring back the first one")
    events = await collect_until(queue, lambda e: isinstance(e.payload, ProcessResumed))

    resumed = events[-1].payload
    assert isinstance(resumed, ProcessResumed)
    assert (resumed.process_id, resumed.title) == (first_id, "first")
    suspends = [e.payload.title for e in events if isinstance(e.payload, ProcessSuspended)]
    assert suspends == ["second"]

    skill.release.set()
    llm.release.set()
    events = await collect_completions(queue, 2)

    done = completions(events)
    assert {item.title for item in done} == {"first", "second"}
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.message.content for item in finished] == ["after"]
    contents = [m.content for m in runner.history()]
    assert contents[:3] == ["first", "second", "bring back the first one"]
    assert "first final" in contents
    assert "after" in contents


async def test_router_cancel_stops_the_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([blocking_call()])
    router = FakeRouter()
    manager = make_manager(
        llm, blocking_registry(skill), session_factory, ManagerOptions(router=router)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)

    def cancel_all(processes: tuple[ProcessInfo, ...], message: str) -> RouteDecision:
        return RouteDecision(
            ops=tuple(
                RouteOp(action=RouteAction.CANCEL, target_id=process.id) for process in processes
            )
        )

    router.handler = cancel_all
    await runner.submit("stop it")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    await asyncio.sleep(0)
    skill.release.set()
    events = await collect_until(queue, is_completed)

    assert any(isinstance(e.payload, Cancelled) for e in events)
    done = completions(events)
    assert [(item.title, item.status) for item in done] == [("start", TaskStatus.CANCELLED.value)]
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="start"),
        ChatMessage(role=MessageRole.USER, content="stop it"),
    ]


async def test_cancel_api_cancels_only_the_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([blocking_call(), blocking_call(), reply("first final")])
    manager = make_manager(llm, blocking_registry(skill), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("first")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await runner.submit("second")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))

    await runner.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    skill.release.set()
    events = await collect_completions(queue, 2)

    by_title = {item.title: item.status for item in completions(events)}
    assert by_title == {"second": TaskStatus.CANCELLED.value, "first": TaskStatus.DONE.value}
    assert any(isinstance(e.payload, Cancelled) for e in events)
    assert not any(isinstance(e.payload, Finished) for e in events)
    contents = [m.content for m in runner.history()]
    assert "first final" in contents


async def test_process_limit_injects_refusal_into_busy_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    manager = make_manager(
        llm, blocking_registry(skill), session_factory, ManagerOptions(max_processes=ONE_PROCESS)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("new question")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_REFUSAL)
    await asyncio.sleep(0)
    skill.release.set()
    events = await collect_completions(queue, 1)

    assert len(completions(events)) == 1
    refusal = runner.history()[2]
    assert refusal.role is MessageRole.SYSTEM
    assert "process limit (1)" in refusal.content
    assert "start" in refusal.content
    second_request = llm.requests[1]
    assert any(
        m.role is MessageRole.SYSTEM and "process limit" in m.content for m in second_request
    )


async def test_process_limit_starts_report_run_when_foreground_is_free(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(
        main=[reply("report answer"), reply("bg reported")],
        background=[reply(TASK_RESULT)],
    )
    llm.gate_background = True
    store = InMemoryTaskStore()
    manager = make_manager(
        llm,
        SkillRegistry(),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    spawned = await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    assert TASK_TITLE in spawned or "task" in spawned

    await runner.submit("new question")
    events = await collect_completions(queue, 1)

    refusal = runner.history()[1]
    assert refusal.role is MessageRole.SYSTEM
    assert "process limit (1)" in refusal.content
    assert TASK_TITLE in refusal.content
    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.message.content for item in finished] == ["report answer"]
    report_branch = llm.main_requests[-1]
    assert report_branch[-2].content == refusal.content
    assert report_branch[-1] == ChatMessage(role=MessageRole.USER, content=REPORT_NUDGE)

    llm.background_release.set()
    events = await collect_completions(queue, 2)

    tasks = await store.list(runner.dialog_id)
    assert [(task.status, task.result_delivered) for task in tasks] == [(TaskStatus.DONE, True)]
    contents = [m.content for m in runner.history()]
    assert any(TASK_RESULT in content for content in contents)


async def test_spawn_task_refuses_over_the_process_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    manager = make_manager(
        llm, blocking_registry(skill), session_factory, ManagerOptions(max_processes=ONE_PROCESS)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)

    refusal = await runner.spawn_task("another job", "do it")

    assert "cannot spawn" in refusal
    assert "process limit (1)" in refusal
    assert "start" in refusal

    skill.release.set()
    await collect_until(queue, is_completed)


async def test_task_spawn_skill_runs_background_process_and_reports(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = SkillRegistry()
    registry.register(TaskSpawnSkill(), SkillOrigin.BASIC)
    llm = BranchLLM(
        main=[task_spawn_call(), reply("spawn confirmed"), reply("report answer")],
        background=[reply(TASK_RESULT)],
    )
    llm.gate_background = True
    store = InMemoryTaskStore()
    manager = make_manager(llm, registry, session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("do it in the background")
    await collect_completions(queue, 1)
    llm.background_release.set()
    events = await collect_completions(queue, 2)

    tasks = await store.list(runner.dialog_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.title == TASK_TITLE
    assert task.status is TaskStatus.DONE
    assert task.result == TASK_RESULT
    assert task.result_delivered is True

    notification = runner.history()[-2]
    assert notification.role is MessageRole.SYSTEM
    assert TASK_TITLE in notification.content
    assert TASK_RESULT in notification.content
    assert runner.history()[-1] == reply("report answer")

    finished = [e.payload for e in events if isinstance(e.payload, Finished)]
    assert [item.message.content for item in finished] == ["report answer"]
    report_request = llm.main_requests[-1]
    assert report_request[-1] == ChatMessage(role=MessageRole.USER, content=REPORT_NUDGE)
    background_request = llm.background_requests[0]
    assert background_request[0].content.startswith(BACKGROUND_TASK_PROMPT)
    assert "Current date and time" in background_request[0].content
    assert background_request[1] == ChatMessage(role=MessageRole.USER, content=TASK_PROMPT)


async def test_task_done_notification_injected_into_busy_foreground(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = BranchLLM(main=[blocking_call(), reply("after")], background=[reply(TASK_RESULT)])
    store = InMemoryTaskStore()
    manager = make_manager(
        llm, blocking_registry(skill), session_factory, ManagerOptions(store=store)
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)

    await runner.spawn_task(TASK_TITLE, TASK_PROMPT)
    await wait_for_condition(lambda: any(TASK_RESULT in m.content for m in runner.history()))
    await asyncio.sleep(0)
    skill.release.set()
    await collect_completions(queue, 2)

    second_request = llm.main_requests[1]
    assert any(m.role is MessageRole.SYSTEM and TASK_RESULT in m.content for m in second_request)
    tasks = await store.list(runner.dialog_id)
    assert [(task.status, task.result_delivered) for task in tasks] == [(TaskStatus.DONE, True)]


async def test_interrupted_turn_is_salvaged_into_the_narrative(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = StallingLLM()
    manager = make_manager(llm, SkillRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("work")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))
    await runner.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    llm.release.set()
    events = await collect_until(queue, is_completed)

    assert any(isinstance(e.payload, Cancelled) for e in events)
    done = completions(events)
    assert [item.status for item in done] == [TaskStatus.CANCELLED.value]
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="work"),
        ChatMessage(role=MessageRole.ASSISTANT, content=PARTIAL),
        ChatMessage(role=MessageRole.SYSTEM, content=INTERRUPTED_NOTE),
    ]
    dialog = await get_dialog(session_factory)
    assert await MessageRepository(session_factory).list(dialog.id) == runner.history()


async def test_injection_at_run_end_gets_own_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = GatedLLM()
    router = FakeRouter()
    manager = make_manager(llm, quick_registry(), session_factory, ManagerOptions(router=router))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))

    router.decide(RouteOp(action=RouteAction.INJECT))
    await runner.submit("extra context")
    await wait_for_condition(lambda: len(runner.history()) == MESSAGES_AFTER_INJECT)
    await asyncio.sleep(0)
    llm.release.set()
    await collect_completions(queue, 2)

    assert len(llm.requests) == EXPECTED_LLM_CALLS
    third_request = llm.requests[2]
    assert any(m.role is MessageRole.USER and m.content == "extra context" for m in third_request)
    assert runner.history() == [
        ChatMessage(role=MessageRole.USER, content="start"),
        ChatMessage(role=MessageRole.USER, content="extra context"),
        reply("first final"),
        reply("after"),
    ]


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

    assert restored.history() == [
        ChatMessage(role=MessageRole.USER, content="start"),
        reply("after"),
    ]


CRON_JOB_ID = "cron-job-1"
CRON_TITLE = "morning report"
CRON_PROMPT = "prepare the daily report"


async def test_wake_runs_cron_tagged_background_process(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = BranchLLM(main=[reply("report answer")], background=[reply(TASK_RESULT)])
    store = InMemoryTaskStore()
    manager = make_manager(llm, SkillRegistry(), session_factory, ManagerOptions(store=store))
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await manager.wake(USER_ID, CHANNEL, CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)
    await collect_completions(queue, 2)

    tasks = await store.list(runner.dialog_id)
    assert len(tasks) == 1
    task = tasks[0]
    assert task.title == CRON_TITLE
    assert task.status is TaskStatus.DONE
    assert task.result == TASK_RESULT
    assert task.input["cron_job_id"] == CRON_JOB_ID
    notification = runner.history()[-2]
    assert notification.role is MessageRole.SYSTEM
    assert CRON_TITLE in notification.content
    assert TASK_RESULT in notification.content
    assert runner.history()[-1] == reply("report answer")
    background_request = llm.background_requests[0]
    assert background_request[0].content.startswith(BACKGROUND_TASK_PROMPT)
    assert background_request[1] == ChatMessage(role=MessageRole.USER, content=CRON_PROMPT)


async def test_wake_over_the_process_limit_publishes_a_system_note(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([blocking_call(), reply("after")])
    store = InMemoryTaskStore()
    manager = make_manager(
        llm,
        blocking_registry(skill),
        session_factory,
        ManagerOptions(store=store, max_processes=ONE_PROCESS),
    )
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)

    await runner.wake(CRON_TITLE, CRON_PROMPT, CRON_JOB_ID)

    await wait_for_condition(lambda: any("could not start" in m.content for m in runner.history()))
    note = runner.history()[-1]
    assert note.role is MessageRole.SYSTEM
    assert f"Cron job '{CRON_TITLE}' could not start" in note.content
    assert "process limit (1)" in note.content
    assert "start" in note.content
    assert await store.list(runner.dialog_id) == []  # no task was created

    skill.release.set()
    await collect_until(queue, is_completed)


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
        SkillRegistry(),
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


async def test_process_slot_released_when_finalize_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = FailingFinalizeStore()
    manager = make_manager(
        ScriptedLLM([reply("bg1"), reply("bg2")]),
        SkillRegistry(),
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
