"""Pydantic models of the Telegram Bot API objects the adapter consumes."""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Telegram photos carry no explicit media type; the Bot API always transcodes
# them to JPEG regardless of the original upload format.
DEFAULT_PHOTO_MEDIA_TYPE = "image/jpeg"
IMAGE_MEDIA_TYPE_PREFIX = "image/"


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


class TelegramPhotoSize(BaseModel):
    """One rendition of an uploaded photo; the Bot API sends several sizes."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_size: int = 0
    width: int = 0
    height: int = 0


class TelegramDocument(BaseModel):
    """A file sent as a document (used here only when it is an image)."""

    model_config = ConfigDict(extra="ignore")

    file_id: str
    mime_type: str = ""
    file_name: str = ""


@dataclass(frozen=True, slots=True)
class TelegramImageRef:
    """A downloadable image found on a message: its file id and media type."""

    file_id: str
    media_type: str


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
    # Telegram sends one entry per rendered size (ascending); an image sent
    # as a document instead of a photo lands here rather than in `photo`
    photo: list[TelegramPhotoSize] = Field(default_factory=list)
    document: TelegramDocument | None = None

    @property
    def body(self) -> str | None:
        """The message's text wherever it lives (plain text or a caption)."""
        return self.text if self.text is not None else self.caption

    @property
    def best_image(self) -> TelegramImageRef | None:
        """The best downloadable image on this message, if any.

        Prefers the largest `photo` rendition (picked by pixel area rather
        than trusting the ascending-order convention blindly); falls back to
        `document` when its declared MIME type says it is an image. None
        when the message carries neither.
        """
        if self.photo:
            largest = max(self.photo, key=lambda size: size.width * size.height)
            return TelegramImageRef(file_id=largest.file_id, media_type=DEFAULT_PHOTO_MEDIA_TYPE)
        if self.document is not None and self.document.mime_type.startswith(
            IMAGE_MEDIA_TYPE_PREFIX
        ):
            return TelegramImageRef(
                file_id=self.document.file_id, media_type=self.document.mime_type
            )
        return None


class TelegramUpdate(BaseModel):
    """One update entry of `getUpdates`."""

    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
