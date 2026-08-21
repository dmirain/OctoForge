"""Typing pulse shown while slow media ingestion runs."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from octoforge_core.domain import MessageKind

from octoforge_telegram.client import (
    ACTIVITY_INTERVAL_SECONDS,
    CHAT_ACTION_TYPING,
    TelegramApiError,
    TelegramClient,
)


class IngestionActivity:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    @asynccontextmanager
    async def showing(self, chat_id: int, kind: MessageKind) -> AsyncIterator[None]:
        if kind is not MessageKind.OWN:
            yield
            return
        pulse = asyncio.create_task(self._pulse(chat_id))
        try:
            yield
        finally:
            pulse.cancel()
            with suppress(asyncio.CancelledError):
                await pulse

    async def _pulse(self, chat_id: int) -> None:
        while True:
            with suppress(TelegramApiError, httpx.HTTPError):
                await self._client.send_chat_action(chat_id, CHAT_ACTION_TYPING)
            await asyncio.sleep(ACTIVITY_INTERVAL_SECONDS)
