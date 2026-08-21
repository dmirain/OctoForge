"""One chat-level typing pulse shared by all in-flight exchanges."""

import asyncio
from contextlib import suppress

import httpx

from octoforge_telegram.client import (
    ACTIVITY_INTERVAL_SECONDS,
    CHAT_ACTION_TYPING,
    TelegramApiError,
    TelegramClient,
)


class TypingIndicator:
    def __init__(self, client: TelegramClient, chat_id: int) -> None:
        self._client = client
        self._chat_id = chat_id
        self._exchanges: set[str | None] = set()
        self._pulse: asyncio.Task[None] | None = None

    @property
    def exchanges(self) -> set[str | None]:
        return self._exchanges

    @property
    def pulse(self) -> asyncio.Task[None] | None:
        return self._pulse

    def start(self, exchange_id: str | None) -> None:
        self._exchanges.add(exchange_id)
        if self._pulse is None or self._pulse.done():
            self._pulse = asyncio.create_task(self._run())

    def stop(self, exchange_id: str | None) -> None:
        self._exchanges.discard(exchange_id)
        if not self._exchanges and self._pulse is not None:
            self._pulse.cancel()
            self._pulse = None

    def stop_all(self) -> None:
        self._exchanges.clear()
        if self._pulse is not None:
            self._pulse.cancel()
            self._pulse = None

    async def _run(self) -> None:
        while True:
            with suppress(TelegramApiError, httpx.HTTPError):
                await self._client.send_chat_action(self._chat_id, CHAT_ACTION_TYPING)
            await asyncio.sleep(ACTIVITY_INTERVAL_SECONDS)
