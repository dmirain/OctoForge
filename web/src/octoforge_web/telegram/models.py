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


class TelegramOriginChat(BaseModel):
    """A chat a forwarded message came from; only its title is shown."""

    model_config = ConfigDict(extra="ignore")

    id: int
    title: str = ""


class TelegramForwardOrigin(BaseModel):
    """Where a forwarded message originally came from.

    One slim model instead of a discriminated union over the four Bot API
    variants (user / hidden_user / chat / channel): each variant only adds
    optional fields, and `extra="ignore"` keeps a future fifth variant
    parseable — an unknown `type` simply yields no attribution name, never a
    parse error that would drop the whole update.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    date: int = 0
    sender_user: TelegramUser | None = None
    sender_user_name: str | None = None
    sender_chat: TelegramOriginChat | None = None
    chat: TelegramOriginChat | None = None

    @property
    def display_name(self) -> str:
        """Who to credit the forwarded text to; empty when the origin is opaque."""
        if self.sender_user is not None:
            name = " ".join(
                part for part in (self.sender_user.first_name, self.sender_user.last_name) if part
            )
            return name or (f"@{self.sender_user.username}" if self.sender_user.username else "")
        if self.sender_user_name:
            return self.sender_user_name
        source = self.sender_chat or self.chat
        return source.title if source is not None else ""


class TelegramMessage(BaseModel):
    """A message payload of an update (`from` is a keyword, hence the alias)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    from_user: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    text: str | None = None
    # a photo/document carries its text as a caption instead
    caption: str | None = None
    # set on every item of an album, so a batch can be collapsed into one entry
    media_group_id: str | None = None
    forward_origin: TelegramForwardOrigin | None = None
    reply_to_message: TelegramReplyToMessage | None = None

    @property
    def body(self) -> str | None:
        """The message's text wherever it lives (plain text or a caption)."""
        return self.text if self.text is not None else self.caption


class TelegramUpdate(BaseModel):
    """One update entry of `getUpdates`."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
