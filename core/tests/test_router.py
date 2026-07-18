"""Tests for the LLMRouter message router."""

import asyncio
from collections.abc import AsyncIterator

from octoforge_core.agent.router import (
    ROUTE_TOOL_NAME,
    LLMRouter,
    ProcessInfo,
    ProcessPlace,
    RouteAction,
    RouteOp,
)
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import StreamEvent
from octoforge_core.skills.base import SkillSpec

FG_ID = "p-fg"
BG_ID = "p-bg"
UNKNOWN_ID = "p-unknown"
MESSAGE = "what about the budget?"
MAX_PROCESSES = 5
TIMEOUT_SECONDS = 0.05
SLOW_LLM_DELAY_SECONDS = 60.0


def foreground() -> ProcessInfo:
    return ProcessInfo(id=FG_ID, title="foreground work", place=ProcessPlace.FOREGROUND)


def background() -> ProcessInfo:
    return ProcessInfo(id=BG_ID, title="background work", place=ProcessPlace.BACKGROUND)


def route_reply(ops: list[dict[str, object]]) -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id="call-1", name=ROUTE_TOOL_NAME, arguments={"ops": ops}),),
    )


def plain_reply() -> ChatMessage:
    return ChatMessage(role=MessageRole.ASSISTANT, content="no tool call")


class ScriptedLLM:
    """LLMClient stub returning a scripted complete() reply or raising."""

    def __init__(self, reply: ChatMessage | None = None, error: Exception | None = None) -> None:
        self._reply = reply
        self._error = error
        self.complete_calls = 0
        self.last_messages: list[ChatMessage] = []
        self.last_tools: list[SkillSpec] | None = None

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        self.complete_calls += 1
        self.last_messages = list(messages)
        self.last_tools = tools
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        return self._reply

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator


class SlowLLM:
    """LLMClient stub whose complete() never answers in time."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        await asyncio.sleep(SLOW_LLM_DELAY_SECONDS)
        raise AssertionError("should have been cancelled by the router timeout")

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator


def make_router(llm: ScriptedLLM | SlowLLM) -> LLMRouter:
    return LLMRouter(llm=llm, timeout_seconds=TIMEOUT_SECONDS)


async def test_empty_snapshot_passes_through_without_llm_call() -> None:
    llm = ScriptedLLM(reply=plain_reply())
    router = make_router(llm)

    decision = await router.route((), MESSAGE, MAX_PROCESSES)

    assert decision.ops == ()
    assert llm.complete_calls == 0


async def test_ops_are_parsed_from_the_route_tool_call() -> None:
    llm = ScriptedLLM(
        reply=route_reply(
            [
                {"action": "inject", "target_id": None},
                {"action": "cancel", "target_id": BG_ID},
                {"action": "promote", "target_id": BG_ID},
                {"action": "start_new", "target_id": None},
            ]
        )
    )
    router = make_router(llm)

    decision = await router.route((foreground(), background()), MESSAGE, MAX_PROCESSES)

    assert decision.ops == (
        RouteOp(action=RouteAction.INJECT),
        RouteOp(action=RouteAction.CANCEL, target_id=BG_ID),
        RouteOp(action=RouteAction.PROMOTE, target_id=BG_ID),
        RouteOp(action=RouteAction.START_NEW),
    )


async def test_request_carries_processes_limit_and_user_message() -> None:
    llm = ScriptedLLM(reply=route_reply([]))
    router = make_router(llm)

    await router.route((foreground(), background()), MESSAGE, MAX_PROCESSES)

    system, user = llm.last_messages
    assert system.role is MessageRole.SYSTEM
    assert FG_ID in system.content and BG_ID in system.content
    assert str(MAX_PROCESSES) in system.content
    assert user == ChatMessage(role=MessageRole.USER, content=MESSAGE)
    assert llm.last_tools is not None
    assert [spec.name for spec in llm.last_tools] == [ROUTE_TOOL_NAME]


async def test_invalid_ops_are_dropped() -> None:
    llm = ScriptedLLM(
        reply=route_reply(
            [
                {"action": "explode", "target_id": None},
                {"action": "inject", "target_id": FG_ID},
                {"action": "start_new", "target_id": BG_ID},
                {"action": "cancel", "target_id": None},
                {"action": "promote", "target_id": UNKNOWN_ID},
                {"action": "cancel", "target_id": BG_ID},
                "not-an-object",
            ]
        )
    )
    router = make_router(llm)

    decision = await router.route((foreground(), background()), MESSAGE, MAX_PROCESSES)

    assert decision.ops == (RouteOp(action=RouteAction.CANCEL, target_id=BG_ID),)


async def test_all_invalid_ops_yield_an_empty_decision() -> None:
    llm = ScriptedLLM(reply=route_reply([{"action": "cancel", "target_id": UNKNOWN_ID}]))
    router = make_router(llm)

    decision = await router.route((foreground(),), MESSAGE, MAX_PROCESSES)

    assert decision.ops == ()


async def test_missing_tool_call_falls_back_to_start_new_with_foreground() -> None:
    llm = ScriptedLLM(reply=plain_reply())
    router = make_router(llm)

    decision = await router.route((foreground(), background()), MESSAGE, MAX_PROCESSES)

    assert decision.ops == (RouteOp(action=RouteAction.START_NEW),)


async def test_missing_tool_call_falls_back_to_empty_without_foreground() -> None:
    llm = ScriptedLLM(reply=plain_reply())
    router = make_router(llm)

    decision = await router.route((background(),), MESSAGE, MAX_PROCESSES)

    assert decision.ops == ()


async def test_llm_error_falls_back() -> None:
    llm = ScriptedLLM(error=RuntimeError("llm down"))
    router = make_router(llm)

    decision = await router.route((foreground(),), MESSAGE, MAX_PROCESSES)

    assert decision.ops == (RouteOp(action=RouteAction.START_NEW),)


async def test_llm_timeout_falls_back() -> None:
    router = make_router(SlowLLM())

    decision = await router.route((background(),), MESSAGE, MAX_PROCESSES)

    assert decision.ops == ()
