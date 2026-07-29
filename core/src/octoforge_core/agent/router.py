"""Message router: decides which exchange an incoming user message belongs to.

An exchange is one obligation to the user (their question, its clarifications
and its answer — see `dialogs/api.py` and `docs/exchanges.md`). Routing is a
single question: *whose is this message?* — which collapses the old
inject/start_new pair into `CONTINUE` vs `NEW`, and needs no conflict
guardrail: a message belongs to exactly one exchange.

Deterministic paths (an explicit reply, no live exchanges) are resolved by the
caller without an LLM call at all; this module is the reasoning fallback.
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from octoforge_core.agent.prompts import ROUTER_PROMPT_NAME, PromptProvider
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.ports import LLMClient
from octoforge_core.tools.base import ToolSpec

logger = logging.getLogger(__name__)


class RouteAction(StrEnum):
    """What the incoming message is, relative to the live exchanges."""

    NEW = "new"
    CONTINUE = "continue"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Where the message belongs, plus exchanges the user asked to stop.

    The default is the safe one: a new exchange. A wrong `new` costs a
    redundant answer the user can see and correct; a wrong `continue` feeds
    the message into someone else's run, where it may never be answered.
    """

    action: RouteAction = RouteAction.NEW
    exchange_id: str | None = None
    cancel_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    """Snapshot of a live exchange handed to the router."""

    id: str
    title: str
    status: ExchangeStatus
    pending_question: str | None = None
    age_seconds: float = 0.0


class MessageRouter(Protocol):
    """Decides which exchange an incoming user message belongs to."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision:
        """Return where the message belongs and what to cancel first."""
        ...


ROUTE_TOOL_NAME = "route"
ROUTE_TOOL_SPEC = ToolSpec(
    name=ROUTE_TOOL_NAME,
    description="Say which exchange the user message belongs to.",
    parameters_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [action.value for action in RouteAction],
                "description": (
                    "new = the message opens its own exchange; "
                    "continue = it belongs to an existing one (an answer to your "
                    "question, a correction, an added detail); "
                    "command = pure control (e.g. 'stop'), nothing to answer."
                ),
            },
            "exchange_id": {
                "type": ["string", "null"],
                "description": "The exchange for continue; null otherwise.",
            },
            "cancel_exchange_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exchanges the user explicitly asked to stop.",
            },
        },
        "required": ["action", "exchange_id", "cancel_exchange_ids"],
    },
)


class LLMRouter:
    """MessageRouter doing a one-shot LLM tool call over the live exchanges.

    The system prompt template comes from the injected `PromptProvider`
    (`ROUTER_PROMPT_NAME`) with the `{limit}` and `{exchanges}` placeholders.
    """

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
        """Route the message; fall back to a new exchange on LLM trouble."""
        if not exchanges:
            # nothing to belong to: a new exchange is the only possible answer
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
        except Exception:  # router failures must not break the dialog
            logger.warning("router LLM call failed; falling back to a new exchange", exc_info=True)
            return RouteDecision()
        reply = completion.message
        call = next((c for c in reply.tool_calls if c.name == ROUTE_TOOL_NAME), None)
        if call is None:
            logger.warning("router answer carried no %s call; falling back", ROUTE_TOOL_NAME)
            return RouteDecision()
        decision = _parse_decision(call.arguments, {item.id for item in exchanges})
        logger.info(
            "routed: action=%s exchange=%s cancels=%s candidates=%s",
            decision.action.value,
            decision.exchange_id,
            len(decision.cancel_ids),
            [item.id for item in exchanges],
        )
        return decision

    def _build_messages(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> list[ChatMessage]:
        lines = "\n".join(_describe(item) for item in exchanges)
        template = self._prompts.get(ROUTER_PROMPT_NAME)
        system = template.format(limit=max_exchanges, exchanges=lines)
        return [
            ChatMessage(role=MessageRole.SYSTEM, content=system),
            ChatMessage(role=MessageRole.USER, content=message),
        ]


def _describe(item: ExchangeInfo) -> str:
    """One human-readable line about a live exchange (the router's whole input)."""
    state = {
        ExchangeStatus.COLLECTING: (
            "material the user forwarded, not answered yet — a message about "
            "that material belongs here"
        ),
        ExchangeStatus.OPEN: "queued",
        ExchangeStatus.IN_PROGRESS: "being answered right now",
        ExchangeStatus.AWAITING_USER: "waiting for the user to reply",
    }.get(item.status, item.status.value)
    line = f'- id={item.id} | "{item.title}" | {state} | {int(item.age_seconds)}s ago'
    if item.status is ExchangeStatus.AWAITING_USER and item.pending_question:
        line += f'\n    you asked: "{item.pending_question}"'
    return line


def _parse_decision(arguments: dict[str, object], known_ids: set[str]) -> RouteDecision:
    """Validate the tool arguments; anything unusable degrades to a new exchange."""
    cancel_ids = tuple(
        value
        for value in _as_list(arguments.get("cancel_exchange_ids"))
        if isinstance(value, str) and value in known_ids
    )
    try:
        action = RouteAction(str(arguments.get("action")))
    except ValueError:
        logger.warning("router action unusable, defaulting to a new exchange: %r", arguments)
        return RouteDecision(cancel_ids=cancel_ids)
    target = arguments.get("exchange_id")
    if action is RouteAction.CONTINUE:
        if not isinstance(target, str) or target not in known_ids:
            logger.warning("router continue without a known exchange: %r", arguments)
            return RouteDecision(cancel_ids=cancel_ids)
        return RouteDecision(action=action, exchange_id=target, cancel_ids=cancel_ids)
    return RouteDecision(action=action, cancel_ids=cancel_ids)


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
