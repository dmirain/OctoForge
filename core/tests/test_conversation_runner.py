"""Tests for ConversationRunner and ConversationManager."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from octoforge_core.agent.errors import ConversationNotFoundError
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    TextDelta,
    ToolCallRequested,
)
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.runner import ConversationEvent, ConversationManager
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.skills.base import SkillContext, SkillOrigin, SkillSpec
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus

PROMPT = "test system prompt"
REPLY = "hello"
BLOCKING_SKILL = "blocking"
CALL_ID = "call-1"
TASK_RESULT = "42"
TIMEOUT_SECONDS = 2.0
MAX_ITERATIONS = 3
EXPECTED_LLM_CALLS = 3
FIRST_CALL = 1
SECOND_CALL = 2


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
        return "unblocked"


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


def make_manager(llm: ScriptedLLM, registry: SkillRegistry) -> ConversationManager:
    loop = AgentLoop(llm_client=llm, registry=registry, max_iterations=MAX_ITERATIONS)
    return ConversationManager(loop=loop, system_prompt=PROMPT)


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


async def test_submit_streams_events_and_updates_history() -> None:
    manager = make_manager(ScriptedLLM([reply()]), SkillRegistry())
    conversation_id = manager.create_conversation()
    runner = manager.get(conversation_id)
    queue = runner.subscribe()

    await runner.submit("hi")
    events = await collect_until(queue, is_terminal)

    assert isinstance(events[-1].payload, Finished)
    seqs = [event.seq for event in events]
    assert seqs == sorted(seqs)
    history = runner.history()
    assert history[0] == ChatMessage(role=MessageRole.USER, content="hi")
    assert history[-1].content == REPLY


async def test_message_during_run_is_injected() -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([assistant_call(), reply("after")])
    manager = make_manager(llm, blocking_registry(skill))
    conversation_id = manager.create_conversation()
    runner = manager.get(conversation_id)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.submit("extra context")
    skill.release.set()
    await collect_until(queue, is_terminal)

    second_request = llm.requests[1]
    assert any(m.role is MessageRole.USER and m.content == "extra context" for m in second_request)


async def test_cancel_stops_run() -> None:
    skill = BlockingSkill()
    llm = ScriptedLLM([assistant_call()])
    manager = make_manager(llm, blocking_registry(skill))
    conversation_id = manager.create_conversation()
    runner = manager.get(conversation_id)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, ToolCallRequested))
    await asyncio.wait_for(skill.started.wait(), timeout=TIMEOUT_SECONDS)
    await runner.cancel()
    skill.release.set()
    events = await collect_until(queue, is_terminal)

    assert isinstance(events[-1].payload, Cancelled)


async def test_task_done_produces_proactive_message() -> None:
    llm = ScriptedLLM([reply("task summary")])
    manager = make_manager(llm, SkillRegistry())
    conversation_id = manager.create_conversation()
    runner = manager.get(conversation_id)
    queue = runner.subscribe()

    task = Task(conversation_id=conversation_id, title="research", kind=TaskKind.PROMPT, input={})
    task.status = TaskStatus.DONE
    task.result = TASK_RESULT
    await manager.notify_task_done(task)
    events = await collect_until(queue, is_terminal)

    assert isinstance(events[-1].payload, Finished)
    system_messages = [m for m in llm.requests[0] if m.role is MessageRole.SYSTEM]
    assert any(TASK_RESULT in m.content for m in system_messages)
    assert any(m.role is MessageRole.SYSTEM and TASK_RESULT in m.content for m in runner.history())


async def test_injection_at_run_end_gets_own_run() -> None:
    llm = GatedLLM()
    registry = SkillRegistry()
    registry.register(QuickSkill(), SkillOrigin.BASIC)
    loop = AgentLoop(llm_client=llm, registry=registry, max_iterations=MAX_ITERATIONS)
    manager = ConversationManager(loop=loop, system_prompt=PROMPT)
    conversation_id = manager.create_conversation()
    runner = manager.get(conversation_id)
    queue = runner.subscribe()

    await runner.submit("start")
    await collect_until(queue, lambda e: isinstance(e.payload, TextDelta))

    task = Task(conversation_id=conversation_id, title="bg", kind=TaskKind.PROMPT, input={})
    task.status = TaskStatus.DONE
    task.result = TASK_RESULT
    await manager.notify_task_done(task)
    llm.release.set()

    await collect_until(queue, is_terminal)
    await collect_until(queue, is_terminal)

    assert len(llm.requests) == EXPECTED_LLM_CALLS
    third_request = llm.requests[2]
    assert any(m.role is MessageRole.SYSTEM and TASK_RESULT in m.content for m in third_request)


async def test_notify_unknown_conversation_is_ignored() -> None:
    manager = make_manager(ScriptedLLM([]), SkillRegistry())
    task = Task(conversation_id="missing", title="t", kind=TaskKind.PROMPT, input={})

    await manager.notify_task_done(task)


def test_get_unknown_conversation_raises() -> None:
    manager = make_manager(ScriptedLLM([]), SkillRegistry())

    with pytest.raises(ConversationNotFoundError):
        manager.get("missing")
