"""Public message router and its LLM-backed implementation."""

import asyncio
import logging
from dataclasses import replace

from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, PromptProvider
from octoforge_core.agent.router_candidates import render_candidates
from octoforge_core.agent.router_contract import ROUTE_TOOL_NAME, ROUTE_TOOL_SPEC
from octoforge_core.agent.router_decision import parse_decision
from octoforge_core.agent.router_types import (
    ExchangeInfo,
    MessageRouter,
    RouteAction,
    RouteDecision,
)
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.ports import LLMClient

__all__ = [
    "ROUTE_TOOL_NAME",
    "ExchangeInfo",
    "LLMRouter",
    "MessageRouter",
    "RouteAction",
    "RouteDecision",
]

logger = logging.getLogger(__name__)


class LLMRouter:
    """One-shot LLM routing over a bounded snapshot of live exchanges."""

    def __init__(self, llm: LLMClient, timeout_seconds: float, prompts: PromptProvider) -> None:
        self._llm = llm
        self._timeout_seconds = timeout_seconds
        self._prompts = prompts

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        if not exchanges:
            logger.debug("routed: no live exchanges, new exchange")
            return RouteDecision()
        try:
            completion = await asyncio.wait_for(
                self._llm.complete(
                    self._build_messages(exchanges, message, max_exchanges),
                    tools=[ROUTE_TOOL_SPEC],
                ),
                timeout=self._timeout_seconds,
            )
        except Exception:
            logger.warning("router LLM call failed; falling back to a new exchange", exc_info=True)
            return RouteDecision()
        call = next(
            (item for item in completion.message.tool_calls if item.name == ROUTE_TOOL_NAME),
            None,
        )
        if call is None:
            logger.warning("router answer carried no %s call; falling back", ROUTE_TOOL_NAME)
            return RouteDecision(usage=completion.usage)
        decision = replace(
            parse_decision(call.arguments, {item.id for item in exchanges}),
            usage=completion.usage,
        )
        logger.info(
            "routed: action=%s exchange=%s cancels=%s title=%r candidates=%s",
            decision.action.value,
            decision.exchange_id,
            len(decision.cancel_ids),
            decision.title,
            [item.id for item in exchanges],
        )
        return decision

    def _build_messages(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> list[ChatMessage]:
        system = self._prompts.get(ROUTER_PROMPT_NAME).format(
            limit=max_exchanges,
            exchanges=render_candidates(exchanges),
        )
        return [
            ChatMessage(role=MessageRole.SYSTEM, content=system),
            ChatMessage(role=MessageRole.USER, content=message),
        ]
