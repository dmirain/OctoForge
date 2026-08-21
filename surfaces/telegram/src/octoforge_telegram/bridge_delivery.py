"""Throttle, split and deliver one Telegram answer draft."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from octoforge_telegram.bridge_drafts import DraftBook
from octoforge_telegram.bridge_format import compose, split_markdown
from octoforge_telegram.bridge_state import Draft
from octoforge_telegram.client import MAX_RICH_MESSAGE_LENGTH, TelegramApiError, TelegramClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryServices:
    user_id: str
    chat_id: int
    client: TelegramClient
    drafts: DraftBook
    record_reply: Callable[[int, str], None]


class DraftDelivery:
    def __init__(self, services: DeliveryServices, throttle_seconds: float) -> None:
        self._user_id = services.user_id
        self._chat_id = services.chat_id
        self._client = services.client
        self._drafts = services.drafts
        self._record_reply = services.record_reply
        self._throttle = throttle_seconds

    async def flush_throttled(self, draft: Draft) -> None:
        now = time.monotonic()
        if draft.pending_since is None:
            draft.pending_since = now
        await self._flush_when_due(draft, now)

    def cancel_timer(self, draft: Draft) -> None:
        if draft.flush_timer is not None:
            draft.flush_timer.cancel()
            draft.flush_timer = None

    async def flush(self, draft: Draft) -> None:
        raw = compose(draft)
        if not raw:
            return
        chunks = split_markdown(raw, MAX_RICH_MESSAGE_LENGTH)
        while draft.sealed_chunks < len(chunks) - 1:
            if chunks[draft.sealed_chunks] != draft.delivered_text:
                await self._deliver(draft, chunks[draft.sealed_chunks])
            draft.message_id = None
            draft.delivered_text = ""
            draft.sealed_chunks += 1
        if chunks[-1] != draft.delivered_text:
            await self._deliver(draft, chunks[-1])

    async def _flush_when_due(self, draft: Draft, now: float) -> None:
        if draft.pending_since is None:
            return
        wait = draft.pending_since + self._throttle - now
        if wait <= 0:
            draft.pending_since = None
            await self.flush(draft)
        elif draft.flush_timer is None or draft.flush_timer.done():
            draft.flush_timer = asyncio.create_task(self._flush_after(draft, wait))

    async def _flush_after(self, draft: Draft, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._flush_when_due(draft, time.monotonic())
        except (TelegramApiError, httpx.HTTPError):
            logger.warning("Telegram deferred flush failed for %s", self._user_id, exc_info=True)

    async def _deliver(self, draft: Draft, markdown: str) -> None:
        if draft.message_id is None:
            reply_to = draft.reply_to if draft.sealed_chunks == 0 else None
            draft.message_id = await self._client.send_rich_message(
                self._chat_id,
                markdown,
                reply_to_message_id=reply_to,
            )
            if draft.exchange_id is not None:
                self._record_reply(draft.message_id, draft.exchange_id)
            await self._drafts.remember(draft)
        else:
            await self._client.edit_message_rich(self._chat_id, draft.message_id, markdown)
        draft.delivered_text = markdown
