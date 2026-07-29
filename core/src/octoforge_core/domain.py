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


class MessageKind(StrEnum):
    """What a user message IS, next to the role that says who sent it.

    OWN is the user speaking to the agent — the only kind that opens an
    obligation. MATERIAL is content the user shared rather than wrote
    (forwarded messages): it is someone else's text, so it never becomes a
    question by itself and never starts a run on its own.
    """

    OWN = "own"
    MATERIAL = "material"


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
    # forwarded material vs the user's own words; drives branch marking and
    # the rule that material never opens an obligation (legacy rows load OWN)
    kind: MessageKind = MessageKind.OWN
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
