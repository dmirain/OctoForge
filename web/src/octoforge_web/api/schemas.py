"""API schemas for dialog endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field

MIN_QUERY_PARAM_LENGTH = 1


class PostMessageRequest(BaseModel):
    """Incoming user message.

    `reply_to_exchange_id` lets a transport that already resolved an explicit
    reply (e.g. a Telegram reply-to) name the exchange outright, skipping the
    LLM router (`ConversationRunner.submit`'s deterministic reply shortcut).
    """

    content: str
    client_message_id: str | None = None
    reply_to_exchange_id: str | None = None


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
