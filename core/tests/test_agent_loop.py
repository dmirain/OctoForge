"""Tests for AgentLoop event streaming."""

from collections.abc import AsyncIterator
from typing import Any

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    TextDelta,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.agent.loop import MAX_ITERATIONS_MESSAGE, AgentLoop
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import StreamEvent, StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.skills.base import SkillContext, SkillOrigin, SkillSpec
from octoforge_core.skills.registry import SkillRegistry

SKILL_NAME = "fake_skill"
SKILL_OUTPUT = "skill output"
FINAL_CONTENT = "done"
CALL_ID = "call-1"
FAILURE_MESSAGE = "boom"
INJECTED_CONTENT = "extra context"
CTX = SkillContext(conversation_id="conv-test")


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


class ChunkedLLM:
    """LLMClient stub yielding content in multiple deltas."""

    def __init__(self, parts: list[str]) -> None:
        self._parts = parts

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        return ChatMessage(role=MessageRole.ASSISTANT, content="".join(self._parts))

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for part in self._parts:
            yield LlmTextDelta(text=part)
        yield StreamFinished(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="".join(self._parts))
        )


class RecordingSkill:
    """Skill stub recording invocations."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=SKILL_NAME, description="test stub", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        self.calls.append(arguments)
        return SKILL_OUTPUT


class FailingSkill:
    """Skill stub raising an error."""

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=SKILL_NAME, description="failing stub", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        raise RuntimeError(FAILURE_MESSAGE)


class InjectingSkill:
    """Skill stub injecting a message into the running loop."""

    def __init__(self, control: LoopControl) -> None:
        self._control = control

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=SKILL_NAME, description="injecting stub", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        self._control.inject(ChatMessage(role=MessageRole.USER, content=INJECTED_CONTENT))
        return SKILL_OUTPUT


def make_registry(skill: object) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(skill, SkillOrigin.BASIC)
    return registry


def assistant_with_call() -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=CALL_ID, name=SKILL_NAME, arguments={"q": 1}),),
    )


def final_reply(content: str = FINAL_CONTENT) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content=content)


def user_message(content: str = "do it") -> ChatMessage:
    return ChatMessage(role=MessageRole.USER, content=content)


async def collect(stream: AsyncIterator[LoopEvent]) -> list[LoopEvent]:
    return [event async for event in stream]


async def test_final_answer_event_sequence() -> None:
    history = [user_message()]
    loop = AgentLoop(
        llm_client=ScriptedLLM([final_reply()]),
        registry=SkillRegistry(),
        max_iterations=3,
    )

    events = await collect(loop.stream(history, LoopControl(), CTX))

    assert isinstance(events[0], IterationStarted)
    assert TextDelta(text=FINAL_CONTENT) in events
    assert isinstance(events[-1], Finished)
    assert events[-1].message == final_reply()
    assert history[-1] == final_reply()


async def test_tool_call_flow_events_and_history() -> None:
    llm = ScriptedLLM([assistant_with_call(), final_reply()])
    skill = RecordingSkill()
    loop = AgentLoop(llm_client=llm, registry=make_registry(skill), max_iterations=3)
    history = [user_message()]

    events = await collect(loop.stream(history, LoopControl(), CTX))

    assert skill.calls == [{"q": 1}]
    assert any(isinstance(event, ToolCallRequested) for event in events)
    assert any(
        isinstance(event, ToolCallCompleted) and event.output == SKILL_OUTPUT for event in events
    )
    assert isinstance(events[-1], Finished)
    tool_messages = [m for m in history if m.role is MessageRole.TOOL]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == CALL_ID
    assert llm.requests[1][-1] == tool_messages[0]


async def test_injected_message_reaches_next_iteration() -> None:
    control = LoopControl()
    llm = ScriptedLLM([assistant_with_call(), final_reply()])
    loop = AgentLoop(
        llm_client=llm,
        registry=make_registry(InjectingSkill(control)),
        max_iterations=3,
    )

    await collect(loop.stream([user_message()], control, CTX))

    second_request = llm.requests[1]
    assert any(m.role is MessageRole.USER and m.content == INJECTED_CONTENT for m in second_request)


async def test_cancel_mid_stream_keeps_partial_message() -> None:
    parts = ["part-1", "part-2", "part-3"]
    control = LoopControl()
    loop = AgentLoop(llm_client=ChunkedLLM(parts), registry=SkillRegistry(), max_iterations=3)
    history = [user_message()]
    events: list[LoopEvent] = []

    async for event in loop.stream(history, control, CTX):
        events.append(event)
        if isinstance(event, TextDelta):
            control.cancel()

    assistant = next(e for e in events if isinstance(e, AssistantMessage))
    assert assistant.interrupted is True
    assert assistant.message.content == "part-1"
    assert isinstance(events[-1], Cancelled)
    assert history[-1].content == "part-1"


async def test_skill_failure_emits_event_and_continues() -> None:
    llm = ScriptedLLM([assistant_with_call(), final_reply()])
    loop = AgentLoop(llm_client=llm, registry=make_registry(FailingSkill()), max_iterations=3)

    events = await collect(loop.stream([user_message()], LoopControl(), CTX))

    failed = next(e for e in events if isinstance(e, ToolCallFailed))
    assert failed.error == FAILURE_MESSAGE
    assert isinstance(events[-1], Finished)


async def test_max_iterations_emits_failed() -> None:
    replies = [assistant_with_call() for _ in range(5)]
    llm = ScriptedLLM(replies)
    loop = AgentLoop(llm_client=llm, registry=make_registry(RecordingSkill()), max_iterations=2)

    events = await collect(loop.stream([user_message()], LoopControl(), CTX))

    assert isinstance(events[-1], Failed)
    assert events[-1].error == MAX_ITERATIONS_MESSAGE
