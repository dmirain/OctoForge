"""Tests for AgentLoop event streaming."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

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
from octoforge_core.agent.loop import (
    CANCELLED_OUTPUT,
    ERROR_OUTPUT_PREFIX,
    MAX_ITERATIONS_MESSAGE,
    STREAM_IDLE_TIMEOUT_MESSAGE,
    AgentLoop,
)
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import StreamEvent, StreamFinished, ToolCallBroken, ToolCallReady
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Completion
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.registry import SkillRegistry

SKILL_NAME = "fake_skill"
SKILL_OUTPUT = "skill output"
FINAL_CONTENT = "done"
CALL_ID = "call-1"
FAILURE_MESSAGE = "boom"
TIMEOUT_CLASS_NAME = "TimeoutError"
INJECTED_CONTENT = "extra context"
SLOW_NAME = "slow_skill"
FAST_NAME = "fast_skill"
SLOW_CALL_ID = "call-slow"
FAST_CALL_ID = "call-fast"
BROKEN_ERROR = "bad json"
IDLE_TIMEOUT_SECONDS = 0.05
STALL_SECONDS = 60.0
PAUSE_SECONDS = 0.05
CTX = SkillContext(user_id="user-test", channel="web", dialog_id="dlg-test")


class ScriptedLLM:
    """LLMClient stub replaying scripted replies as streams."""

    def __init__(self, replies: list[ChatMessage]) -> None:
        self._replies = list(replies)
        self.requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        self.requests.append(list(messages))
        return Completion(message=self._replies.pop(0))

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
    ) -> Completion:
        return Completion(
            message=ChatMessage(role=MessageRole.ASSISTANT, content="".join(self._parts))
        )

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


class EmptyFailingSkill:
    """Skill stub raising an exception whose str() is empty."""

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=SKILL_NAME, description="empty failing stub", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        raise TimeoutError


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


class EagerLLM:
    """LLMClient stub emitting incremental tool-call events on the first call."""

    def __init__(self, first_events: list[StreamEvent], reply: ChatMessage) -> None:
        self._first_events = first_events
        self._reply = reply
        self.requests: list[list[ChatMessage]] = []

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        return Completion(message=self._reply)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            for event in self._first_events:
                await asyncio.sleep(0)  # let spawned tool tasks run between events
                yield event
        else:
            yield StreamFinished(message=self._reply)


class StallingLLM:
    """LLMClient stub stalling forever after its scripted events."""

    def __init__(self, first_events: list[StreamEvent]) -> None:
        self._first_events = first_events

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        return Completion(message=final_reply())

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for event in self._first_events:
            yield event
        await asyncio.sleep(STALL_SECONDS)


class CrashingLLM:
    """LLMClient stub raising a transport error after its scripted events."""

    def __init__(self, first_events: list[StreamEvent]) -> None:
        self._first_events = first_events

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        return Completion(message=final_reply())

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        for event in self._first_events:
            await asyncio.sleep(0)  # let spawned tool tasks start
            yield event
        raise ConnectionError("transport lost")


class SlowLLM:
    """LLMClient stub pausing before the terminal event."""

    def __init__(self, pause: float, reply: ChatMessage) -> None:
        self._pause = pause
        self._reply = reply

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        return Completion(message=self._reply)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield LlmTextDelta(text=self._reply.content)
        await asyncio.sleep(self._pause)
        yield StreamFinished(message=self._reply)


class NamedSkill:
    """Skill stub with a configurable name and a delay before answering."""

    def __init__(self, name: str, delay: float) -> None:
        self._name = name
        self._delay = delay
        self.calls: list[dict[str, Any]] = []

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=self._name, description="named stub", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        self.calls.append(arguments)
        await asyncio.sleep(self._delay)
        return f"{SKILL_OUTPUT} {self._name}"


class GatedSkill:
    """Skill stub blocking forever; records its cancellation."""

    def __init__(self) -> None:
        self.cancelled = False

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(name=SKILL_NAME, description="gated stub", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return SKILL_OUTPUT


def make_registry(skill: object) -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(skill)
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
    assert failed.error == f"RuntimeError: {FAILURE_MESSAGE}"
    assert isinstance(events[-1], Finished)


async def test_skill_failure_with_empty_message_uses_class_name() -> None:
    llm = ScriptedLLM([assistant_with_call(), final_reply()])
    loop = AgentLoop(llm_client=llm, registry=make_registry(EmptyFailingSkill()), max_iterations=3)

    events = await collect(loop.stream([user_message()], LoopControl(), CTX))

    failed = next(e for e in events if isinstance(e, ToolCallFailed))
    assert failed.error == TIMEOUT_CLASS_NAME
    tool_message = next(m for m in llm.requests[1] if m.role == MessageRole.TOOL)
    assert tool_message.content == f"{ERROR_OUTPUT_PREFIX}{TIMEOUT_CLASS_NAME}"
    assert isinstance(events[-1], Finished)


async def test_max_iterations_emits_failed() -> None:
    replies = [assistant_with_call() for _ in range(5)]
    llm = ScriptedLLM(replies)
    loop = AgentLoop(llm_client=llm, registry=make_registry(RecordingSkill()), max_iterations=2)

    events = await collect(loop.stream([user_message()], LoopControl(), CTX))

    assert isinstance(events[-1], Failed)
    assert events[-1].error == MAX_ITERATIONS_MESSAGE


def eager_call(call_id: str = CALL_ID, name: str = SKILL_NAME) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments={"q": 1})


def eager_first_message(tool_calls: tuple[ToolCall, ...]) -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=tool_calls)


async def test_tool_call_requested_before_stream_finished() -> None:
    call = eager_call()
    llm = EagerLLM(
        [
            ToolCallReady(call=call),
            LlmTextDelta(text="tail"),
            StreamFinished(message=eager_first_message((call,))),
        ],
        final_reply(),
    )
    skill = RecordingSkill()
    loop = AgentLoop(llm_client=llm, registry=make_registry(skill), max_iterations=3)

    events = await collect(loop.stream([user_message()], LoopControl(), CTX))

    requested_at = next(i for i, e in enumerate(events) if isinstance(e, ToolCallRequested))
    assistant_at = next(i for i, e in enumerate(events) if isinstance(e, AssistantMessage))
    assert requested_at < assistant_at
    assert skill.calls == [{"q": 1}]
    assert isinstance(events[-1], Finished)


async def test_eager_calls_run_concurrently_history_keeps_call_order() -> None:
    slow_call = eager_call(SLOW_CALL_ID, SLOW_NAME)
    fast_call = eager_call(FAST_CALL_ID, FAST_NAME)
    llm = EagerLLM(
        [
            ToolCallReady(call=slow_call),
            ToolCallReady(call=fast_call),
            StreamFinished(message=eager_first_message((slow_call, fast_call))),
        ],
        final_reply(),
    )
    registry = SkillRegistry()
    registry.register(NamedSkill(SLOW_NAME, delay=PAUSE_SECONDS))
    registry.register(NamedSkill(FAST_NAME, delay=0.0))
    loop = AgentLoop(llm_client=llm, registry=registry, max_iterations=3)
    history = [user_message()]

    events = await collect(loop.stream(history, LoopControl(), CTX))

    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert [e.call.id for e in completed] == [FAST_CALL_ID, SLOW_CALL_ID]
    tool_messages = [m for m in history if m.role is MessageRole.TOOL]
    assert [m.tool_call_id for m in tool_messages] == [SLOW_CALL_ID, FAST_CALL_ID]
    assert isinstance(events[-1], Finished)


async def test_broken_tool_call_reports_error_without_execution() -> None:
    call = eager_call()
    llm = EagerLLM(
        [
            ToolCallBroken(
                index=0, call_id=CALL_ID, name=SKILL_NAME, error=BROKEN_ERROR, raw='{"q": '
            ),
            StreamFinished(message=eager_first_message((call,))),
        ],
        final_reply(),
    )
    skill = RecordingSkill()
    loop = AgentLoop(llm_client=llm, registry=make_registry(skill), max_iterations=3)
    history = [user_message()]

    events = await collect(loop.stream(history, LoopControl(), CTX))

    assert skill.calls == []
    failed = next(e for e in events if isinstance(e, ToolCallFailed))
    assert failed.error == BROKEN_ERROR
    tool_message = next(m for m in history if m.role is MessageRole.TOOL)
    assert tool_message.content == f"{ERROR_OUTPUT_PREFIX}{BROKEN_ERROR}"
    assert tool_message.tool_call_id == CALL_ID
    assert isinstance(events[-1], Finished)


async def test_cancel_mid_stream_cancels_running_tool() -> None:
    call = eager_call()
    control = LoopControl()
    skill = GatedSkill()
    llm = EagerLLM(
        [ToolCallReady(call=call), LlmTextDelta(text="more"), LlmTextDelta(text="even more")],
        final_reply(),
    )
    loop = AgentLoop(llm_client=llm, registry=make_registry(skill), max_iterations=3)
    history = [user_message()]
    events: list[LoopEvent] = []

    async for event in loop.stream(history, control, CTX):
        events.append(event)
        if isinstance(event, ToolCallRequested):
            control.cancel()

    assistant = next(e for e in events if isinstance(e, AssistantMessage))
    assert assistant.interrupted is True
    assert assistant.message.tool_calls == (call,)
    tool_message = next(m for m in history if m.role is MessageRole.TOOL)
    assert tool_message.content == CANCELLED_OUTPUT
    assert tool_message.tool_call_id == CALL_ID
    assert skill.cancelled is True
    assert isinstance(events[-1], Cancelled)


async def test_cancel_after_tool_completed_keeps_result() -> None:
    call = eager_call()
    control = LoopControl()
    llm = EagerLLM(
        [ToolCallReady(call=call), LlmTextDelta(text="more"), LlmTextDelta(text="even more")],
        final_reply(),
    )
    loop = AgentLoop(llm_client=llm, registry=make_registry(RecordingSkill()), max_iterations=3)
    history = [user_message()]
    events: list[LoopEvent] = []

    async for event in loop.stream(history, control, CTX):
        events.append(event)
        if isinstance(event, ToolCallCompleted):
            control.cancel()

    assistant = next(e for e in events if isinstance(e, AssistantMessage))
    assert assistant.interrupted is True
    assert assistant.message.tool_calls == (call,)
    tool_message = next(m for m in history if m.role is MessageRole.TOOL)
    assert tool_message.content == SKILL_OUTPUT
    assert isinstance(events[-1], Cancelled)


async def test_idle_timeout_fails_run_and_cancels_tools() -> None:
    skill = GatedSkill()
    llm = StallingLLM([ToolCallReady(call=eager_call()), LlmTextDelta(text="chunk")])
    loop = AgentLoop(
        llm_client=llm,
        registry=make_registry(skill),
        max_iterations=3,
        stream_idle_timeout=IDLE_TIMEOUT_SECONDS,
    )
    history = [user_message()]

    events = await collect(loop.stream(history, LoopControl(), CTX))

    failed = next(e for e in events if isinstance(e, Failed))
    assert failed.error == STREAM_IDLE_TIMEOUT_MESSAGE
    assert skill.cancelled is True
    assert history == [user_message()]


async def test_stream_exception_aborts_running_tools() -> None:
    skill = GatedSkill()
    llm = CrashingLLM([ToolCallReady(call=eager_call()), LlmTextDelta(text="tail")])
    loop = AgentLoop(llm_client=llm, registry=make_registry(skill), max_iterations=3)
    history = [user_message()]

    with pytest.raises(ConnectionError, match="transport lost"):
        await collect(loop.stream(history, LoopControl(), CTX))

    assert skill.cancelled is True  # the eager tool run did not outlive the failed run
    assert history == [user_message()]


async def test_idle_timeout_before_first_event() -> None:
    llm = StallingLLM([])
    loop = AgentLoop(
        llm_client=llm,
        registry=SkillRegistry(),
        max_iterations=3,
        stream_idle_timeout=IDLE_TIMEOUT_SECONDS,
    )

    events = await collect(loop.stream([user_message()], LoopControl(), CTX))

    assert isinstance(events[-1], Failed)
    assert events[-1].error == STREAM_IDLE_TIMEOUT_MESSAGE


async def test_disabled_idle_timeout_keeps_waiting() -> None:
    llm = SlowLLM(pause=PAUSE_SECONDS, reply=final_reply())
    loop = AgentLoop(llm_client=llm, registry=SkillRegistry(), max_iterations=3)

    events = await collect(loop.stream([user_message()], LoopControl(), CTX))

    assert isinstance(events[-1], Finished)
    assert events[-1].message == final_reply()
