"""Telegram Bot API port, message commands and shared constants."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from octoforge_telegram.models import TelegramUpdate

TELEGRAM_CHANNEL = "telegram"
USER_ID_PREFIX = "tg:"
MAX_MESSAGE_LENGTH = 4096
MAX_RICH_MESSAGE_LENGTH = 32768
CHAT_ACTION_TYPING = "typing"
ACTIVITY_INTERVAL_SECONDS = 4.0


class TelegramApiError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SendMessage:
    chat_id: int
    text: str
    parse_mode: str | None = None
    reply_to_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class EditMessage:
    chat_id: int
    message_id: int
    text: str
    parse_mode: str | None = None


class TelegramClient(Protocol):
    async def get_updates(
        self,
        offset: int | None,
        timeout_seconds: float,
    ) -> list[TelegramUpdate]: ...

    async def send_message(self, request: SendMessage) -> int: ...

    async def edit_message_text(self, request: EditMessage) -> None: ...

    async def send_rich_message(
        self,
        chat_id: int,
        markdown: str,
        reply_to_message_id: int | None = None,
    ) -> int: ...

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None: ...

    async def send_chat_action(self, chat_id: int, action: str) -> None: ...

    async def get_file(self, file_id: str) -> str: ...

    async def download_file(self, file_path: str) -> bytes: ...

    async def set_my_commands(self, commands: Sequence[tuple[str, str]]) -> None: ...
