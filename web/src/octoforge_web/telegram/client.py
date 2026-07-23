"""Minimal Telegram Bot API client over plain httpx."""

import logging
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from octoforge_web.telegram.models import TelegramUpdate

TELEGRAM_CHANNEL = "telegram"
USER_ID_PREFIX = "tg:"
MAX_MESSAGE_LENGTH = 4096
MAX_RICH_MESSAGE_LENGTH = 32768
API_BASE_URL = "https://api.telegram.org"
CHAT_ACTION_TYPING = "typing"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
LONG_POLL_TIMEOUT_MARGIN_SECONDS = 10.0
NOT_MODIFIED_MARKER = "message is not modified"
CANT_PARSE_ENTITIES_MARKER = "can't parse entities"
ALLOWED_UPDATES = ("message",)

logger = logging.getLogger(__name__)


class TelegramApiError(Exception):
    """The Bot API answered `ok=false` for a method call."""


class TelegramClient(Protocol):
    """Port of the Telegram Bot API used by the adapter."""

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[TelegramUpdate]:
        """Fetch updates after `offset`, long-polling up to `timeout_seconds`."""
        ...

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> int:
        """Send a text message; return its message id."""
        ...

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        """Replace the text of an existing message."""
        ...

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        """Upgrade an existing message to a Rich Message from raw Markdown."""
        ...

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        """Send a chat action (e.g. typing)."""
        ...


class TelegramBotClient:
    """Bot API client: token in the method path, JSON payloads, long-poll timeouts."""

    def __init__(self, http_client: httpx.AsyncClient, token: str) -> None:
        self._http = http_client
        self._base_url = f"{API_BASE_URL}/bot{token}"

    async def get_updates(self, offset: int | None, timeout_seconds: float) -> list[TelegramUpdate]:
        payload: dict[str, Any] = {"timeout": timeout_seconds, "allowed_updates": ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        result = await self._call(
            "getUpdates",
            payload,
            timeout=timeout_seconds + LONG_POLL_TIMEOUT_MARGIN_SECONDS,
        )
        if not isinstance(result, list):
            raise TelegramApiError("getUpdates: unexpected result shape")
        return _parse_updates(result)

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        result = await self._call_with_parse_fallback("sendMessage", payload)
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramApiError("sendMessage: unexpected result shape")
        return int(result["message_id"])

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, parse_mode: str | None = None
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        try:
            await self._call_with_parse_fallback("editMessageText", payload)
        except TelegramApiError as exc:
            if NOT_MODIFIED_MARKER not in str(exc):
                raise

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    async def edit_message_rich(self, chat_id: int, message_id: int, markdown: str) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"markdown": markdown},
        }
        try:
            await self._call("editMessageText", payload)
        except TelegramApiError as exc:
            if NOT_MODIFIED_MARKER not in str(exc):
                raise

    async def _call_with_parse_fallback(
        self, method: str, payload: dict[str, Any]
    ) -> dict[str, Any] | list[Any]:
        """Call `method`, retrying without parse_mode when the API rejects the markup."""
        try:
            return await self._call(method, payload)
        except TelegramApiError as exc:
            if "parse_mode" not in payload or CANT_PARSE_ENTITIES_MARKER not in str(exc):
                raise
            logger.warning("%s rejected the markup; retrying as plain text", method)
            plain = {key: value for key, value in payload.items() if key != "parse_mode"}
            return await self._call(method, plain)

    async def _call(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any] | list[Any]:
        response = await self._http.post(
            f"{self._base_url}/{method}", json=payload, timeout=timeout
        )
        body = _parse_body(response)
        if body is None:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                # The request URL carries the bot token; keep it out of logs.
                raise TelegramApiError(f"{method}: HTTP {exc.response.status_code}") from None
            raise TelegramApiError(f"{method}: unexpected response shape")
        # Error statuses still carry a JSON body with the failure description.
        if not body.get("ok"):
            description = body.get("description", "unknown error")
            raise TelegramApiError(f"{method}: {description}")
        result = body.get("result")
        return result if isinstance(result, (dict, list)) else {}


def _parse_body(response: httpx.Response) -> dict[str, Any] | None:
    """Parse the response JSON object, or None when the body is not one."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _parse_updates(items: list[Any]) -> list[TelegramUpdate]:
    """Parse raw update dicts, skipping entries that do not match the schema.

    A poison entry is reduced to its bare update id (message=None) so the
    poller still advances the offset past it instead of re-fetching forever.
    """
    updates: list[TelegramUpdate] = []
    for item in items:
        try:
            updates.append(TelegramUpdate.model_validate(item))
            continue
        except ValidationError:
            pass
        if isinstance(item, dict) and isinstance(item.get("update_id"), int):
            logger.warning("Dropping an unparsable Telegram update %s", item["update_id"])
            updates.append(TelegramUpdate(update_id=item["update_id"]))
        else:
            logger.warning("Dropping a Telegram update without an update id")
    return updates
