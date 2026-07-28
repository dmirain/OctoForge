"""Tests for the LLMRouter message router."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest

from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, StaticPromptProvider
from octoforge_core.agent.router import (
    ROUTE_TOOL_NAME,
    ExchangeInfo,
    LLMRouter,
    RouteAction,
)
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import StreamEvent
from octoforge_core.llm.usage import Completion
from octoforge_core.tools.base import ToolSpec

OPEN_ID = "x-open"
WAITING_ID = "x-waiting"
UNKNOWN_ID = "x-unknown"
MESSAGE = "what about the budget?"
MAX_EXCHANGES = 5
TIMEOUT_SECONDS = 0.05
SLOW_LLM_DELAY_SECONDS = 60.0
CUSTOM_ROUTER_PROMPT = "CUSTOM ROUTER: limit {limit}; exchanges:\n{exchanges}"
ROUTER_LOGGER = "octoforge_core.agent.router"


PENDING_QUESTION = "In which city?"
STALE_AGE_SECONDS = 90.0


def in_progress() -> ExchangeInfo:
    return ExchangeInfo(id=OPEN_ID, title="the budget report", status=ExchangeStatus.IN_PROGRESS)


def stale_in_progress() -> ExchangeInfo:
    return ExchangeInfo(
        id=OPEN_ID,
        title="the budget report",
        status=ExchangeStatus.IN_PROGRESS,
        age_seconds=STALE_AGE_SECONDS,
    )


def awaiting_user() -> ExchangeInfo:
    return ExchangeInfo(
        id=WAITING_ID,
        title="What is the weather?",
        status=ExchangeStatus.AWAITING_USER,
        pending_question=PENDING_QUESTION,
    )


def route_reply(
    action: str = "new",
    exchange_id: str | None = None,
    cancel: list[str] | None = None,
) -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=(
            ToolCall(
                id="call-1",
                name=ROUTE_TOOL_NAME,
                arguments={
                    "action": action,
                    "exchange_id": exchange_id,
                    "cancel_exchange_ids": cancel or [],
                },
            ),
        ),
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
        self.last_tools: list[ToolSpec] | None = None

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        self.complete_calls += 1
        self.last_messages = list(messages)
        self.last_tools = tools
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        return Completion(message=self._reply)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator


class SlowLLM:
    """LLMClient stub whose complete() never answers in time."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        await asyncio.sleep(SLOW_LLM_DELAY_SECONDS)
        raise AssertionError("should have been cancelled by the router timeout")

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator


def make_router(llm: ScriptedLLM | SlowLLM) -> LLMRouter:
    return LLMRouter(
        llm=llm,
        timeout_seconds=TIMEOUT_SECONDS,
        prompts=StaticPromptProvider(),
    )


async def test_no_live_exchanges_skips_the_llm() -> None:
    """Nothing to belong to: a new exchange is the only possible answer."""
    llm = ScriptedLLM(reply=route_reply())
    router = make_router(llm)

    decision = await router.route((), MESSAGE, MAX_EXCHANGES)

    assert decision.action is RouteAction.NEW
    assert llm.complete_calls == 0


async def test_continue_into_a_known_exchange() -> None:
    llm = ScriptedLLM(reply=route_reply(action="continue", exchange_id=WAITING_ID))
    router = make_router(llm)

    decision = await router.route((in_progress(), awaiting_user()), "Moscow", MAX_EXCHANGES)

    assert decision.action is RouteAction.CONTINUE
    assert decision.exchange_id == WAITING_ID


async def test_continue_into_an_unknown_exchange_degrades_to_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Safe default: a redundant answer is visible, a swallowed message is not."""
    llm = ScriptedLLM(reply=route_reply(action="continue", exchange_id=UNKNOWN_ID))
    router = make_router(llm)

    with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
        decision = await router.route((in_progress(),), MESSAGE, MAX_EXCHANGES)

    assert decision.action is RouteAction.NEW
    assert decision.exchange_id is None
    assert caplog.records


async def test_unknown_action_degrades_to_new_keeping_cancels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = ScriptedLLM(reply=route_reply(action="nonsense", cancel=[OPEN_ID, UNKNOWN_ID]))
    router = make_router(llm)

    with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
        decision = await router.route((in_progress(),), MESSAGE, MAX_EXCHANGES)

    assert decision.action is RouteAction.NEW
    assert decision.cancel_ids == (OPEN_ID,)  # the unknown id is dropped
    assert caplog.records


async def test_command_action_answers_nothing() -> None:
    llm = ScriptedLLM(reply=route_reply(action="command", cancel=[OPEN_ID]))
    router = make_router(llm)

    decision = await router.route((in_progress(),), "stop", MAX_EXCHANGES)

    assert decision.action is RouteAction.COMMAND
    assert decision.cancel_ids == (OPEN_ID,)


async def test_llm_failure_falls_back_to_new(caplog: pytest.LogCaptureFixture) -> None:
    llm = ScriptedLLM(error=RuntimeError("provider down"))
    router = make_router(llm)

    with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
        decision = await router.route((in_progress(),), MESSAGE, MAX_EXCHANGES)

    assert decision.action is RouteAction.NEW
    assert caplog.records


async def test_timeout_falls_back_to_new() -> None:
    router = make_router(SlowLLM())

    decision = await router.route((in_progress(),), MESSAGE, MAX_EXCHANGES)

    assert decision.action is RouteAction.NEW


async def test_missing_tool_call_falls_back_to_new(caplog: pytest.LogCaptureFixture) -> None:
    llm = ScriptedLLM(reply=plain_reply())
    router = make_router(llm)

    with caplog.at_level(logging.WARNING, logger=ROUTER_LOGGER):
        decision = await router.route((in_progress(),), MESSAGE, MAX_EXCHANGES)

    assert decision.action is RouteAction.NEW
    assert caplog.records


async def test_prompt_describes_the_exchanges_in_human_terms() -> None:
    """The router reasons about obligations, not process ids."""
    llm = ScriptedLLM(reply=route_reply())
    router = make_router(llm)

    await router.route((in_progress(), awaiting_user()), MESSAGE, MAX_EXCHANGES)

    system = llm.last_messages[0].content
    assert "being answered right now" in system
    assert "waiting for the user to reply" in system
    assert PENDING_QUESTION in system  # the reply target is obvious to the model
    assert "When you are unsure, choose new" in system
    assert llm.last_messages[1] == ChatMessage(role=MessageRole.USER, content=MESSAGE)


async def test_prompt_renders_the_exchanges_staleness_in_seconds() -> None:
    """`_describe` (the router's whole view of an exchange) states its age."""
    llm = ScriptedLLM(reply=route_reply())
    router = make_router(llm)

    await router.route((stale_in_progress(),), MESSAGE, MAX_EXCHANGES)

    system = llm.last_messages[0].content
    assert f"{int(STALE_AGE_SECONDS)}s ago" in system


async def test_router_prompt_comes_from_the_prompt_provider() -> None:
    llm = ScriptedLLM(reply=route_reply())
    router = LLMRouter(
        llm=llm,
        timeout_seconds=TIMEOUT_SECONDS,
        prompts=StaticPromptProvider({ROUTER_PROMPT_NAME: CUSTOM_ROUTER_PROMPT}),
    )

    await router.route((in_progress(),), MESSAGE, MAX_EXCHANGES)

    assert llm.last_messages[0].content.startswith("CUSTOM ROUTER: limit 5")
