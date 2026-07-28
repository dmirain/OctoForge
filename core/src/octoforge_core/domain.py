"""Domain objects for chat and dialogs."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class MessageRole(StrEnum):
    """Roles allowed in a chat message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A single chat message.

    `task_id` links an assistant message to the background task that produced
    it (NULL for plain narrative messages); it is persisted metadata, not part
    of the LLM-facing payload. `id` is the persisted row id, filled in by the
    store on load and by the actor right after persisting — pure database
    metadata, excluded from equality so constructed and loaded messages
    compare equal; `exchange_id` is the same kind of metadata.
    """

    role: MessageRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    task_id: str | None = None
    id: str | None = field(default=None, compare=False)
    # the exchange (obligation to the user) this message belongs to; database
    # metadata like `id`, excluded from equality
    exchange_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Dialog:
    """A conversation line owned by a user on a channel surface."""

    id: str
    user_id: str
    channel: str
    created_at: datetime
    updated_at: datetime
