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


class AttachmentKind(StrEnum):
    """What a message carries besides its text."""

    IMAGE = "image"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file that came with a message, kept as a reference, not as bytes.

    `ref` is transport-scoped and resolved back to bytes by the surface that
    produced it (Telegram keeps a bot's files indefinitely, so `tg:<file_id>`
    stays valid without us storing anything). The model never sees the ref:
    it reads the description that was written into the message text.
    """

    kind: AttachmentKind
    ref: str


@dataclass(frozen=True, slots=True)
class MessageSource:
    """What an incoming user message is and where it came from.

    Bundled so `submit()` keeps a small signature: transports fill this in
    (a forward carries `MATERIAL` plus its `origin`; an image carries the
    attachment whose description is already in the text), the actor reads it.
    """

    kind: MessageKind = MessageKind.OWN
    origin: str | None = None
    attachments: tuple[Attachment, ...] = ()


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
    # files that came with the message, kept as transport-scoped references
    # (never bytes): the text the model reads is a description, and the
    # reference lets a tool look at the same picture again later
    attachments: tuple[Attachment, ...] = ()
    id: str | None = field(default=None, compare=False)
    # the exchange (obligation to the user) this message belongs to; database
    # metadata like `id`, excluded from equality
    exchange_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class Dialog:
    """A conversation line owned by a user on a channel surface.

    `updated_at` is when this ROW was last written, which is almost never:
    the row is an identity anchor and nothing on it changes after creation.
    It is NOT when the dialog was last active — that used to be its meaning,
    paid for with a write on every message, and it is now read from the
    messages themselves (`last_activity_by_channel`, `last_message_at`).
    """

    id: str
    user_id: str
    channel: str
    created_at: datetime
    updated_at: datetime
