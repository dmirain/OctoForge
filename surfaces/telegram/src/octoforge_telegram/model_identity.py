"""Telegram users, chats, replies and forward origins."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class TelegramChatType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class TelegramUser(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    first_name: str = ""
    last_name: str = ""
    username: str | None = None


def display_name(user: TelegramUser) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part)


class TelegramChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    type: TelegramChatType


class TelegramReplyToMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message_id: int


class TelegramOriginChat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str = ""


class TelegramForwardOrigin(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    date: int = 0
    sender_user: TelegramUser | None = None
    sender_user_name: str | None = None
    sender_chat: TelegramOriginChat | None = None
    chat: TelegramOriginChat | None = None

    @property
    def display_name(self) -> str:
        if self.sender_user is not None:
            name = display_name(self.sender_user)
            return name or (f"@{self.sender_user.username}" if self.sender_user.username else "")
        if self.sender_user_name:
            return self.sender_user_name
        source = self.sender_chat or self.chat
        return source.title if source is not None else ""
