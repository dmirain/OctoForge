"""Long-poll loop and safe handoff into per-user ingestion workers."""

import asyncio
import logging

import httpx
from octoforge_core.identity.api import UserStatus

from octoforge_telegram.client import USER_ID_PREFIX, TelegramApiError, TelegramClient
from octoforge_telegram.gateway import AccessRefusedError, GatewayRegistry
from octoforge_telegram.models import TelegramChatType, TelegramMessage, TelegramUpdate
from octoforge_telegram.poller_inbox import InboxWorkers
from octoforge_telegram.poller_menu import drain_backlog, register_commands
from octoforge_telegram.poller_setup import build_handlers
from octoforge_telegram.poller_types import TelegramPollerOptions

logger = logging.getLogger(__name__)


class TelegramPoller:
    def __init__(
        self,
        client: TelegramClient,
        registry: GatewayRegistry,
        options: TelegramPollerOptions,
    ) -> None:
        self._client = client
        self._registry = registry
        self._options = options
        self._poll_timeout_seconds = options.poll_timeout_seconds
        self._error_backoff_seconds = options.error_backoff_seconds
        self._access, self._commands, self._dispatcher = build_handlers(
            client,
            registry,
            options,
        )
        self._workers = InboxWorkers(self._handle_safely, options.album_quiet_seconds)
        self._inboxes = self._workers.inboxes
        self._offset: int | None = None

    async def run_forever(self) -> None:
        await self._register_commands()
        self._offset = await drain_backlog(self._client)
        try:
            while True:
                try:
                    await self._poll_once()
                except Exception:
                    logger.exception("Telegram poll loop hit an unexpected error; retrying")
                    await asyncio.sleep(self._error_backoff_seconds)
        finally:
            self._workers.close()

    async def dispatch(self, update: TelegramUpdate) -> None:
        message = update.message
        if message is not None and message.from_user is not None:
            self._workers.enqueue(f"{USER_ID_PREFIX}{message.from_user.id}", message)

    async def _poll_once(self) -> None:
        try:
            updates = await self._client.get_updates(self._offset, self._poll_timeout_seconds)
        except (httpx.HTTPError, TelegramApiError):
            await asyncio.sleep(self._error_backoff_seconds)
            return
        for update in updates:
            self._offset = update.update_id + 1
            await self._dispatch_safely(update)

    async def _handle_safely(self, user_id: str, batch: list[TelegramMessage]) -> None:
        try:
            message = batch[0]
            if message.chat.type is not TelegramChatType.PRIVATE or await self._access.gate(
                user_id, message.chat.id, message
            ):
                await self._dispatcher.dispatch(batch, user_id)
        except asyncio.CancelledError:
            raise
        except AccessRefusedError as refusal:
            try:
                status = UserStatus(refusal.status)
            except ValueError:
                logger.warning("service refused %s with unknown status %r", user_id, refusal.status)
            else:
                await self._access.say_status(user_id, batch[0].chat.id, status)
        except Exception:
            logger.warning("Failed to handle Telegram message from %s", user_id, exc_info=True)

    async def _dispatch_safely(self, update: TelegramUpdate) -> None:
        try:
            await self.dispatch(update)
        except Exception:
            logger.warning("Failed to dispatch Telegram update %s", update.update_id, exc_info=True)

    async def _register_commands(self) -> None:
        await register_commands(self._client, self._options)
