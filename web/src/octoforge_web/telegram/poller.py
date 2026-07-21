"""Long-poll loop consuming Telegram updates and dispatching them to chat bridges."""

import asyncio
import logging
from collections.abc import Iterable

import httpx

from octoforge_web.telegram.bridge import RunnerProvider, TelegramBridge
from octoforge_web.telegram.client import (
    USER_ID_PREFIX,
    TelegramApiError,
    TelegramClient,
)
from octoforge_web.telegram.models import TelegramChatType, TelegramUpdate

logger = logging.getLogger(__name__)

COMMAND_START = "/start"
COMMAND_CANCEL = "/cancel"
GREETING_TEXT = (
    "Привет! Я OctoForge — напиши вопрос, и я постараюсь помочь. "
    "Команда /cancel прерывает текущий ответ."
)
TEXT_ONLY_NOTICE = "Пока понимаю только текстовые сообщения."
GROUP_NOTICE = "Пока работаю только в личных чатах."
DEFAULT_ERROR_BACKOFF_SECONDS = 5.0
DRAIN_TIMEOUT_SECONDS = 0.0
LAST_UPDATE_OFFSET = -1


class TelegramBridgeRegistry:
    """Owns one bridge per Telegram user id."""

    def __init__(
        self,
        runner_provider: RunnerProvider,
        client: TelegramClient,
        edit_throttle_seconds: float,
    ) -> None:
        self._runner_provider = runner_provider
        self._client = client
        self._edit_throttle_seconds = edit_throttle_seconds
        self._bridges: dict[str, TelegramBridge] = {}

    def get_or_create(self, user_id: str, chat_id: int) -> TelegramBridge:
        """Return the bridge for the user, creating it on first contact."""
        bridge = self._bridges.get(user_id)
        if bridge is None:
            bridge = TelegramBridge(
                user_id=user_id,
                chat_id=chat_id,
                runner_provider=self._runner_provider,
                client=self._client,
                edit_throttle_seconds=self._edit_throttle_seconds,
            )
            self._bridges[user_id] = bridge
        return bridge

    async def warm(self, user_ids: Iterable[str]) -> None:
        """Start bridges for known Telegram dialogs so their notifications are delivered."""
        for user_id in user_ids:
            chat_id = chat_id_from_user_id(user_id)
            if chat_id is None:
                continue
            await self.get_or_create(user_id, chat_id).start()

    async def aclose(self) -> None:
        """Stop all bridges (on app shutdown)."""
        for bridge in self._bridges.values():
            await bridge.aclose()


class TelegramPoller:
    """Long-poll loop: fetch updates, advance the offset, dispatch to bridges."""

    def __init__(
        self,
        client: TelegramClient,
        registry: TelegramBridgeRegistry,
        poll_timeout_seconds: float,
        error_backoff_seconds: float = DEFAULT_ERROR_BACKOFF_SECONDS,
    ) -> None:
        self._client = client
        self._registry = registry
        self._poll_timeout_seconds = poll_timeout_seconds
        self._error_backoff_seconds = error_backoff_seconds
        self._offset: int | None = None

    async def run_forever(self) -> None:
        """Poll updates until cancelled; transient failures only log and back off."""
        await self._drain_backlog()
        while True:
            try:
                updates = await self._client.get_updates(self._offset, self._poll_timeout_seconds)
            except (httpx.HTTPError, TelegramApiError):
                logger.warning("Telegram polling failed; retrying", exc_info=True)
                await asyncio.sleep(self._error_backoff_seconds)
                continue
            for update in updates:
                self._offset = update.update_id + 1
                await self._dispatch_safely(update)

    async def dispatch(self, update: TelegramUpdate) -> None:
        """Route one update: commands, non-text/group notices, or text into the bridge."""
        message = update.message
        if message is None or message.from_user is None:
            return
        chat_id = message.chat.id
        if message.chat.type is not TelegramChatType.PRIVATE:
            await self._client.send_message(chat_id, GROUP_NOTICE)
            return
        if message.text is None:
            await self._client.send_message(chat_id, TEXT_ONLY_NOTICE)
            return
        if message.text == COMMAND_START:
            await self._client.send_message(chat_id, GREETING_TEXT)
            return
        bridge = self._registry.get_or_create(
            user_id=f"{USER_ID_PREFIX}{message.from_user.id}", chat_id=chat_id
        )
        if message.text == COMMAND_CANCEL:
            await bridge.cancel()
            return
        await bridge.handle_text(message.text, client_message_id=str(update.update_id))

    async def _dispatch_safely(self, update: TelegramUpdate) -> None:
        try:
            await self.dispatch(update)
        except (httpx.HTTPError, TelegramApiError):
            logger.warning("Failed to dispatch Telegram update %s", update.update_id, exc_info=True)

    async def _drain_backlog(self) -> None:
        """Skip updates that accumulated while the app was down (best effort)."""
        try:
            updates = await self._client.get_updates(LAST_UPDATE_OFFSET, DRAIN_TIMEOUT_SECONDS)
        except (httpx.HTTPError, TelegramApiError):
            logger.warning("Failed to drain the Telegram update backlog", exc_info=True)
            return
        if updates:
            self._offset = updates[-1].update_id + 1


def chat_id_from_user_id(user_id: str) -> int | None:
    """Derive the private chat id from a tg-prefixed user id (None when malformed)."""
    if not user_id.startswith(USER_ID_PREFIX):
        return None
    try:
        return int(user_id.removeprefix(USER_ID_PREFIX))
    except ValueError:
        return None
