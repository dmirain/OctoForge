"""Typed message-routing boundary shared by actors and router implementations."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.llm.usage import Usage


class RouteAction(StrEnum):
    """What the incoming message is, relative to the live exchanges."""

    NEW = "new"
    CONTINUE = "continue"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The selected exchange, requested cancellations and optional new title."""

    action: RouteAction = RouteAction.NEW
    exchange_id: str | None = None
    cancel_ids: tuple[str, ...] = ()
    title: str | None = None
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    """Snapshot of one live exchange available to the routing decision."""

    id: str
    title: str
    status: ExchangeStatus
    pending_question: str | None = None
    age_seconds: float = 0.0
    preview: str | None = None


class MessageRouter(Protocol):
    """Decides which exchange an incoming user message belongs to."""

    async def route(
        self,
        exchanges: tuple[ExchangeInfo, ...],
        message: str,
        max_exchanges: int,
    ) -> RouteDecision: ...
