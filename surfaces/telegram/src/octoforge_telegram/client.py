"""Minimal Telegram Bot API client over bounded retrying httpx transport."""

from collections.abc import Sequence
from typing import Any

import httpx

from octoforge_telegram.bot_messages import BotMessages
from octoforge_telegram.bot_response import parse_updates
from octoforge_telegram.bot_transport import (
    MAX_DOWNLOADED_FILE_BYTES,
    BotCall,
    BotTransport,
)
from octoforge_telegram.client_types import (
    ACTIVITY_INTERVAL_SECONDS,
    CHAT_ACTION_TYPING,
    MAX_MESSAGE_LENGTH,
    MAX_RICH_MESSAGE_LENGTH,
    TELEGRAM_CHANNEL,
    USER_ID_PREFIX,
    EditMessage,
    SendMessage,
    TelegramApiError,
    TelegramClient,
)
from octoforge_telegram.models import TelegramUpdate

API_BASE_URL = "https://api.telegram.org"
LONG_POLL_TIMEOUT_MARGIN_SECONDS = 10.0
ALLOWED_UPDATES = ("message",)

__all__ = [
    "ACTIVITY_INTERVAL_SECONDS",
    "CHAT_ACTION_TYPING",
    "MAX_DOWNLOADED_FILE_BYTES",
    "MAX_MESSAGE_LENGTH",
    "MAX_RICH_MESSAGE_LENGTH",
    "TELEGRAM_CHANNEL",
    "USER_ID_PREFIX",
    "EditMessage",
    "SendMessage",
    "TelegramApiError",
    "TelegramBotClient",
    "TelegramClient",
]


class TelegramBotClient:
    def __init__(self, http_client: httpx.AsyncClient, token: str) -> None:
        base = f"{API_BASE_URL}/bot{token}"
        files = f"{API_BASE_URL}/file/bot{token}"
        self._transport = BotTransport(http_client, base, files)
        self._messages = BotMessages(self._transport)

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[TelegramUpdate]:
        payload: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ALLOWED_UPDATES,
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self._transport.call(
            BotCall(
                "getUpdates",
                payload,
                timeout_seconds + LONG_POLL_TIMEOUT_MARGIN_SECONDS,
            )
        )
        if not isinstance(result, list):
            raise TelegramApiError("getUpdates: unexpected result shape")
        return parse_updates(result)

    async def send_message(self, request: SendMessage) -> int:
        return await self._messages.send(request)

    async def edit_message_text(self, request: EditMessage) -> None:
        await self._messages.edit(request)

    async def send_rich_message(
        self,
        chat_id: int,
        markdown: str,
        reply_to_message_id: int | None = None,
    ) -> int:
        return await self._messages.send_rich(chat_id, markdown, reply_to_message_id)

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        await self._messages.edit_rich(chat_id, message_id, markdown)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self._transport.call(
            BotCall("sendChatAction", {"chat_id": chat_id, "action": action}, retry=False)
        )

    async def get_file(self, file_id: str) -> str:
        result = await self._transport.call(BotCall("getFile", {"file_id": file_id}))
        if not isinstance(result, dict) or "file_path" not in result:
            raise TelegramApiError("getFile: unexpected result shape")
        return str(result["file_path"])

    async def download_file(self, file_path: str) -> bytes:
        return await self._transport.download(file_path)

    async def set_my_commands(self, commands: Sequence[tuple[str, str]]) -> None:
        payload = {
            "commands": [
                {"command": name, "description": description} for name, description in commands
            ]
        }
        await self._transport.call(BotCall("setMyCommands", payload))
