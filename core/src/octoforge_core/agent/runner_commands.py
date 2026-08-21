"""Commands and delivery records serialized by a dialog actor."""

from dataclasses import dataclass
from enum import StrEnum

from octoforge_core.agent.events import Failed, Finished, LoopEvent
from octoforge_core.agent.router import RouteDecision
from octoforge_core.dialogs.api import Exchange, ExchangeList, ExchangeStatus
from octoforge_core.domain import ChatMessage
from octoforge_core.tasks.api import Task


@dataclass(frozen=True, slots=True)
class Submit:
    message: ChatMessage
    client_message_id: str | None = None
    reply_to_exchange_id: str | None = None
    cancel_epoch: int = 0
    origin: str | None = None


@dataclass(frozen=True, slots=True)
class RouteApplication:
    message: ChatMessage
    decision: RouteDecision
    command: Submit
    live: ExchangeList


@dataclass(frozen=True, slots=True)
class RouteTarget:
    exchange_id: str | None
    created: Exchange | None = None
    refused: bool = False


@dataclass(frozen=True, slots=True)
class Flush:
    """A fresh subscriber requesting an outbox drain."""


class Unseen(StrEnum):
    NONE = "none"
    MATERIAL_ONLY = "material_only"
    SPOKEN = "spoken"


@dataclass(frozen=True, slots=True)
class PromoteCollected:
    exchange_id: str


@dataclass(frozen=True, slots=True)
class ProcessTerminated:
    task_id: str
    terminal: Finished | Failed | None = None
    exchange_id: str | None = None
    delivered_live: bool = False
    exchange_status: ExchangeStatus | None = None
    task: Task | None = None
    unseen_messages: Unseen = Unseen.NONE


@dataclass(frozen=True, slots=True, eq=False)
class Delivery:
    """One finished outcome waiting for transport delivery."""

    events: tuple[LoopEvent, ...]
    task_id: str | None
    exchange_id: str | None = None


Command = Submit | ProcessTerminated | Flush | PromoteCollected
