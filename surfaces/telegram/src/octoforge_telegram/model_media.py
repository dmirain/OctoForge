"""Telegram media payloads and downloadable references."""

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict


class TelegramPhotoSize(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_id: str
    file_size: int = 0
    width: int = 0
    height: int = 0


class TelegramDocument(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_id: str
    mime_type: str = ""
    file_name: str = ""


class TelegramVoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_id: str
    duration: int = 0
    mime_type: str = ""
    file_name: str = ""


@dataclass(frozen=True, slots=True)
class TelegramAudioRef:
    file_id: str
    duration_seconds: int | None
    media_type: str
    file_name: str


@dataclass(frozen=True, slots=True)
class TelegramImageRef:
    file_id: str
    media_type: str
