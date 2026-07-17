"""Tests for ConversationRunner and ConversationManager."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    TextDelta,
    ToolCallRequested,
)
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.runner import ConversationEvent, ConversationManager
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.domain import ChatMessage, Dialog, MessageRole, ToolCall
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.ports import LLMClient, TaskStore
from octoforge_core.skills.base import SkillContext, SkillOrigin, SkillSpec
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import InMemoryTaskStore

PROMPT = "test system prompt"
REPLY = "hello"
BLOCKING_SKILL = "blocking"
CALL_ID = "call-1"
TASK_RESULT = "42"
UNBLOCKED_OUTPUT = "unblocked"
TIMEOUT_SECONDS = 2.0
MAX_ITERATIONS = 3
EXPECTED_LLM_CALLS = 3
FIRST_CALL = 1
SECOND_CALL = 2
USER_ID = "user-1"
CHANNEL = "web"
MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


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
            yield StreamFinished(message=assistant_call())
        elif call_number == SECOND_CALL:
            yield LlmTextDelta(text="partial")
            await self.release.wait()
            yield StreamFinished(message=reply("first final"))
        else:
            yield StreamFinished(message=reply("after"))


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


def assistant_call() -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=CALL_ID, name=BLOCKING_SKILL, arguments={}),),
    )


def reply(content: str = REPLY) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def make_task(dialog_id: str, title: str = "research") -> Task:
    task = Task(
        dialog_id=dialog_id,
        user_id=USER_ID,
        channel=CHANNEL,
        title=title,
        kind=TaskKind.PROMPT,
        input={},
    )
    task.status = TaskStatus.DONE
    task.result = TASK_RESULT
    return task


async def make_manager(
    llm: LLMClient,
    registry: SkillRegistry,
    session_factory: async_sessionmaker[AsyncSession],
    store: TaskStore | None = None,
) -> ConversationManager:
    loop = AgentLoop(llm_client=llm, registry=registry, max_iterations=MAX_ITERATIONS)
    return ConversationManager(
        loop=loop,
        system_prompt=PROMPT,
        dialogs=DialogRepository(session_factory),
        messages=MessageRepository(session_factory),
        tasks=store if store is not None else InMemoryTaskStore(),
    )


async def get_dialog(session_factory: async_sessionmaker[AsyncSession]) -> Dialog:
    return await DialogRepository(session_factory).get_or_create(USER_ID, CHANNEL)


def blocking_registry(skill: BlockingSkill) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(skill, SkillOrigin.BASIC)
    return registry


def is_terminal(event: ConversationEvent) -> bool:
    return isinstance(event.payload, (Finished, Failed, Cancelled))


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


async def test_submit_streams_events_and_updates_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    manager = await make_manager(ScriptedLLM([reply()]), SkillRegistry(), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_terminal)

    assert isinstance(events[-1].payload, Finished)
    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs)
    history = runner.history()
    assert history[0] == ChatMessage(role=MessageRole.USER, content="hi")
    assert history[-1].content == REPLY


async def test_message_during_run_is_injected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([assistant_call(), reply("after")])
    manager = await make_manager(llm, blocking_registry(skill), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("extra context")
    skill.release.set()
    await collect_until(queue, is_terminal)

    second_request = llm.requests[1]
    assert any(m.role is MessageRole.USER and m.content == "extra context" for m in second_request)


async def test_cancel_stops_run(session_factory: async_sessionmaker[AsyncSession]) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([assistant_call()])
    manager = await make_manager(llm, blocking_registry(skill), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.cancel()
    skill.release.set()
    events = await collect_until(queue, is_terminal)

    assert isinstance(events[-1].payload, Cancelled)


async def test_task_done_produces_proactive_message_and_is_marked_delivered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
    llm = ScriptedLLM([reply("task summary")])
    manager = await make_manager(llm, SkillRegistry(), session_factory, store=store)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    dialog = await get_dialog(session_factory)
    task = make_task(dialog.id)
    await store.add(task)
    await manager.notify_task_done(task)
    events = await collect_until(queue, is_terminal)

    assert isinstance(events[-1].payload, Finished)
    system_messages = [m for m in llm.requests[0] if m.role is MessageRole.SYSTEM]
    assert any(TASK_RESULT in m.content for m in system_messages)
    assert any(m.role is MessageRole.SYSTEM and TASK_RESULT in m.content for m in runner.history())
    assert (await store.get(task.id)).result_delivered is True


async def test_notify_task_for_unknown_dialog_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = InMemoryTaskStore()
    manager = await make_manager(ScriptedLLM([]), SkillRegistry(), session_factory, store=store)
    task = make_task("missing-dialog")
    await store.add(task)

    await manager.notify_task_done(task)

    assert (await store.get(task.id)).result_delivered is False


async def test_injection_at_run_end_gets_own_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = GatedLLM()
    registry = SkillRegistry()
    registry.register(QuickSkill(), SkillOrigin.BASIC)
    store = InMemoryTaskStore()
    manager = await make_manager(llm, registry, session_factory, store=store)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))

    dialog = await get_dialog(session_factory)
    task = make_task(dialog.id, title="bg")
    await store.add(task)
    await manager.notify_task_done(task)
    llm.release.set()

    await collect_until(queue, is_terminal)
    await collect_until(queue, is_terminal)

    assert len(llm.requests) == EXPECTED_LLM_CALLS
    third_request = llm.requests[2]
    assert any(m.role is MessageRole.SYSTEM and TASK_RESULT in m.content for m in third_request)


async def test_user_message_is_persisted_before_run_finishes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([assistant_call(), reply("done")])
    manager = await make_manager(llm, blocking_registry(skill), session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)

    dialog = await get_dialog(session_factory)
    stored = await MessageRepository(session_factory).list(dialog.id)
    assert stored == [ChatMessage(role=MessageRole.USER, content="start"), assistant_call()]

    skill.release.set()
    await collect_until(queue, is_terminal)

    assert await MessageRepository(session_factory).list(dialog.id) == [
        ChatMessage(role=MessageRole.USER, content="start"),
        assistant_call(),
        ChatMessage(role=MessageRole.TOOL, content=UNBLOCKED_OUTPUT, tool_call_id=CALL_ID),
        reply("done"),
    ]


async def test_history_is_rebuilt_after_manager_restart(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    registry = SkillRegistry()
    registry.register(QuickSkill(), SkillOrigin.BASIC)
    llm = ScriptedLLM([assistant_call(), reply("after")])
    manager = await make_manager(llm, registry, session_factory)
    runner = await manager.get_or_create_runner(USER_ID, CHANNEL)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, is_terminal)

    restarted = await make_manager(ScriptedLLM([]), registry, session_factory)
    restored = await restarted.get_or_create_runner(USER_ID, CHANNEL)

    assert restored.history() == [
        ChatMessage(role=MessageRole.USER, content="start"),
        assistant_call(),
        ChatMessage(role=MessageRole.TOOL, content="ok", tool_call_id=CALL_ID),
        reply("after"),
    ]
