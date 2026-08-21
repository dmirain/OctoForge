"""Typed commands for message persistence and exchange settlement."""

from dataclasses import dataclass

from octoforge_core.dialogs.types import ExchangeStatus
from octoforge_core.domain import ChatMessage
from octoforge_core.llm.usage import Usage


@dataclass(frozen=True, slots=True)
class MessageAppend:
    dialog_id: str
    message: ChatMessage
    usage: Usage | None = None
    client_message_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExchangeSettlement:
    exchange_id: str
    task_id: str
    status: ExchangeStatus
    keep_if_awaiting: bool = False
