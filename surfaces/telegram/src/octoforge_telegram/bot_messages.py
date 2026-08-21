"""Telegram text and Rich Message send/edit operations."""

from typing import Any

from octoforge_telegram.bot_transport import BotCall, BotTransport
from octoforge_telegram.client_types import EditMessage, SendMessage, TelegramApiError

NOT_MODIFIED_MARKER = "message is not modified"
CANT_PARSE_ENTITIES_MARKER = "can't parse entities"


class BotMessages:
    def __init__(self, transport: BotTransport) -> None:
        self._transport = transport

    async def send(self, request: SendMessage) -> int:
        payload: dict[str, Any] = {"chat_id": request.chat_id, "text": request.text}
        if request.parse_mode is not None:
            payload["parse_mode"] = request.parse_mode
        _add_reply(payload, request.reply_to_message_id)
        result = await self._call_with_parse_fallback("sendMessage", payload)
        return _message_id("sendMessage", result)

    async def edit(self, request: EditMessage) -> None:
        payload: dict[str, Any] = {
            "chat_id": request.chat_id,
            "message_id": request.message_id,
            "text": request.text,
        }
        if request.parse_mode is not None:
            payload["parse_mode"] = request.parse_mode
        try:
            await self._call_with_parse_fallback("editMessageText", payload)
        except TelegramApiError as exc:
            if NOT_MODIFIED_MARKER not in str(exc):
                raise

    async def send_rich(
        self,
        chat_id: int,
        markdown: str,
        reply_to_message_id: int | None,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "rich_message": {"markdown": markdown},
        }
        _add_reply(payload, reply_to_message_id)
        result = await self._transport.call(BotCall("sendRichMessage", payload))
        return _message_id("sendRichMessage", result)

    async def edit_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": markdown},
        }
        try:
            await self._transport.call(BotCall("editMessageText", payload))
        except TelegramApiError as exc:
            if NOT_MODIFIED_MARKER not in str(exc):
                raise

    async def _call_with_parse_fallback(
        self,
        method: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | list[Any]:
        try:
            return await self._transport.call(BotCall(method, payload))
        except TelegramApiError as exc:
            if "parse_mode" not in payload or CANT_PARSE_ENTITIES_MARKER not in str(exc):
                raise
            plain = {key: value for key, value in payload.items() if key != "parse_mode"}
            return await self._transport.call(BotCall(method, plain))


def _add_reply(payload: dict[str, Any], message_id: int | None) -> None:
    if message_id is not None:
        payload["reply_parameters"] = {
            "message_id": message_id,
            "allow_sending_without_reply": True,
        }


def _message_id(method: str, result: object) -> int:
    if not isinstance(result, dict) or "message_id" not in result:
        raise TelegramApiError(f"{method}: unexpected result shape")
    return int(result["message_id"])
