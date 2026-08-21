"""Pydantic models of Telegram Bot API updates consumed by the adapter."""

from pydantic import BaseModel, ConfigDict, Field

from octoforge_telegram.model_identity import (
    TelegramChat,
    TelegramChatType,
    TelegramForwardOrigin,
    TelegramOriginChat,
    TelegramReplyToMessage,
    TelegramUser,
    display_name,
)
from octoforge_telegram.model_media import (
    TelegramAudioRef,
    TelegramDocument,
    TelegramImageRef,
    TelegramPhotoSize,
    TelegramVoice,
)

DEFAULT_PHOTO_MEDIA_TYPE = "image/jpeg"
IMAGE_MEDIA_TYPE_PREFIX = "image/"
AUDIO_MEDIA_TYPE_PREFIX = "audio/"
DEFAULT_VOICE_MEDIA_TYPE = "audio/ogg"
DEFAULT_VOICE_FILE_NAME = "voice.ogg"
DEFAULT_VIDEO_NOTE_FILE_NAME = "note.mp4"
DEFAULT_AUDIO_FILE_NAME = "audio.mp3"

__all__ = [
    "TelegramAudioRef",
    "TelegramChat",
    "TelegramChatType",
    "TelegramDocument",
    "TelegramForwardOrigin",
    "TelegramImageRef",
    "TelegramMessage",
    "TelegramOriginChat",
    "TelegramPhotoSize",
    "TelegramReplyToMessage",
    "TelegramUpdate",
    "TelegramUser",
    "TelegramVoice",
    "display_name",
]


class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    message_id: int
    from_user: TelegramUser | None = Field(default=None, alias="from")
    chat: TelegramChat
    text: str | None = None
    caption: str | None = None
    media_group_id: str | None = None
    forward_origin: TelegramForwardOrigin | None = None
    reply_to_message: TelegramReplyToMessage | None = None
    photo: list[TelegramPhotoSize] = Field(default_factory=list)
    document: TelegramDocument | None = None
    voice: TelegramVoice | None = None
    video_note: TelegramVoice | None = None
    audio: TelegramVoice | None = None

    @property
    def body(self) -> str | None:
        return self.text if self.text is not None else self.caption

    @property
    def best_image(self) -> TelegramImageRef | None:
        if self.photo:
            largest = max(self.photo, key=lambda size: size.width * size.height)
            return TelegramImageRef(largest.file_id, DEFAULT_PHOTO_MEDIA_TYPE)
        if self.document is not None and self.document.mime_type.startswith(
            IMAGE_MEDIA_TYPE_PREFIX
        ):
            return TelegramImageRef(self.document.file_id, self.document.mime_type)
        return None

    @property
    def best_audio(self) -> TelegramAudioRef | None:
        for candidate, fallback_name in (
            (self.voice, DEFAULT_VOICE_FILE_NAME),
            (self.video_note, DEFAULT_VIDEO_NOTE_FILE_NAME),
            (self.audio, DEFAULT_AUDIO_FILE_NAME),
        ):
            if candidate is not None:
                return TelegramAudioRef(
                    candidate.file_id,
                    candidate.duration,
                    candidate.mime_type or DEFAULT_VOICE_MEDIA_TYPE,
                    candidate.file_name or fallback_name,
                )
        if self.document is not None and self.document.mime_type.startswith(
            AUDIO_MEDIA_TYPE_PREFIX
        ):
            return TelegramAudioRef(
                self.document.file_id,
                None,
                self.document.mime_type,
                self.document.file_name or DEFAULT_AUDIO_FILE_NAME,
            )
        return None


class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    update_id: int
    message: TelegramMessage | None = None
