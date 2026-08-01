"""API schemas for dialog endpoints."""

from datetime import datetime

from octoforge_core.domain import Attachment, AttachmentKind, MessageKind, MessageSource
from pydantic import BaseModel, Field

MIN_QUERY_PARAM_LENGTH = 1


class AttachmentSpec(BaseModel):
    """A file that came with the message, as a transport-scoped reference.

    The reference means nothing to core: whoever ingested the file decides
    how it reads back (see the `ImageResolver` port).
    """

    kind: AttachmentKind
    ref: str


class PostMessageRequest(BaseModel):
    """Incoming user message.

    `reply_to_exchange_id` lets a transport that already resolved an explicit
    reply (e.g. a Telegram reply-to) name the exchange outright, skipping the
    LLM router (`ConversationRunner.submit`'s deterministic reply shortcut).

    `kind`, `origin` and `attachments` carry what a *surface* knows about the
    message and core cannot infer: forwarded content is MATERIAL and owes no
    answer of its own, `origin` names whom it came from, and attachments are
    the files that arrived with it. They exist on the API so that a surface
    can live in its own process and still submit everything an in-process one
    could — which is the whole point of putting a router in front of several.
    """

    content: str
    client_message_id: str | None = None
    reply_to_exchange_id: str | None = None
    kind: MessageKind = MessageKind.OWN
    origin: str | None = None
    attachments: tuple[AttachmentSpec, ...] = ()

    def to_source(self) -> MessageSource:
        """What the actor needs to know about where this message came from."""
        return MessageSource(
            kind=self.kind,
            origin=self.origin,
            attachments=tuple(
                Attachment(kind=item.kind, ref=item.ref) for item in self.attachments
            ),
        )


class AckResponse(BaseModel):
    """Accepted-for-processing acknowledgement."""

    status: str


class CronJobCreateParams(BaseModel):
    """Query-string parameters of cron job creation (external_call has no body)."""

    title: str = Field(min_length=MIN_QUERY_PARAM_LENGTH)
    schedule: str = Field(min_length=MIN_QUERY_PARAM_LENGTH)
    prompt: str = Field(min_length=MIN_QUERY_PARAM_LENGTH)
    timezone: str = Field(default="UTC", min_length=MIN_QUERY_PARAM_LENGTH)
    one_shot: bool = False


class CronJobResponse(BaseModel):
    """Wire view of a cron job; datetimes serialize as ISO 8601 UTC."""

    id: str
    user_id: str
    channel: str
    title: str
    schedule: str
    timezone: str
    prompt: str
    enabled: bool
    next_fire_at: datetime
    last_fire_at: datetime | None
    created_at: datetime
    one_shot: bool
    last_status: str | None
    last_error: str | None
    retry_count: int
