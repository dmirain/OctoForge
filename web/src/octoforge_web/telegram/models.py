"""Pydantic models of the Telegram Bot API objects the adapter consumes."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TelegramChatType(StrEnum):
    """Chat types delivered in updates."""

    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class TelegramUser(BaseModel):
    """A Telegram user; unknown API fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    id: int
    first_name: str = ""
    last_name: str = ""
    username: str | None = None


class TelegramChat(BaseModel):
    """A chat an update message was sent to."""

    model_config = ConfigDict(extra="ignore")

    id: int
    type: TelegramChatType


class TelegramReplyToMessage(BaseModel):
    """The replied-to message, slimmed to just its id.

    The Bot API's `reply_to_message` field carries the full message object,
    which is itself a `TelegramMessage` (and could in principle nest further
    replies); the bridge only ever needs the id to resolve a reply target, so
    this stays a minimal model rather than a recursive `TelegramMessage`.
    """

    model_config = ConfigDict(extra="ignore")

    message_id: int


class TelegramMessage(BaseModel):
    """A message payload of an update (`from` is a keyword, hence the alias)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    from_user: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    text: str | None = None
    reply_to_message: TelegramReplyToMessage | None = None


class TelegramUpdate(BaseModel):
    """One update entry of `getUpdates`."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
