"""Fold runner events into Telegram drafts and terminal deliveries."""

import asyncio
import logging
from dataclasses import dataclass

import httpx
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    LoopEvent,
    ProcessStarted,
    ReasoningDelta,
    TextDelta,
    ToolCallRequested,
)

from octoforge_telegram.bridge_delivery import DraftDelivery
from octoforge_telegram.bridge_drafts import DraftBook
from octoforge_telegram.bridge_format import (
    count_status,
    notice_line,
    reply_target,
    tool_group,
)
from octoforge_telegram.bridge_state import (
    CANCELLED_LINE,
    FAILED_LINE_TEMPLATE,
    TERMINAL_FLUSH_RETRY_DELAY_SECONDS,
    THINKING_LABEL,
    Draft,
    StatusEntry,
)
from octoforge_telegram.bridge_typing import TypingIndicator
from octoforge_telegram.client import TelegramApiError

logger = logging.getLogger("octoforge_telegram.bridge")


@dataclass(frozen=True, slots=True)
class RenderServices:
    user_id: str
    drafts: DraftBook
    delivery: DraftDelivery
    typing: TypingIndicator


class EventRenderer:
    def __init__(self, services: RenderServices) -> None:
        self._user_id = services.user_id
        self._drafts = services.drafts
        self._delivery = services.delivery
        self._typing = services.typing

    async def render_safely(self, event: LoopEvent, exchange_id: str | None) -> None:
        try:
            await self._render(event, exchange_id)
        except (TelegramApiError, httpx.HTTPError):
            logger.warning("Telegram render failed for %s", self._user_id, exc_info=True)

    async def _render(self, event: LoopEvent, exchange_id: str | None) -> None:
        draft = self._drafts.get(exchange_id)
        if isinstance(event, ProcessStarted):
            if draft.message_id is None:
                draft.reply_to = reply_target(event.source_client_message_id)
            self._typing.start(exchange_id)
        elif isinstance(event, ReasoningDelta):
            count_status(draft, THINKING_LABEL)
            await self._delivery.flush_throttled(draft)
        elif isinstance(event, TextDelta):
            draft.buffer += event.text
            await self._delivery.flush_throttled(draft)
        elif isinstance(event, (Finished, Cancelled, Failed)):
            await self._terminal(event, exchange_id)
        elif isinstance(event, ToolCallRequested):
            count_status(draft, tool_group(event.call.name))
            await self._delivery.flush_throttled(draft)
        elif (line := notice_line(event)) is not None:
            draft.status.insert(0, StatusEntry(line, counted=False))
            await self._delivery.flush_throttled(draft)

    async def _terminal(
        self,
        event: Finished | Cancelled | Failed,
        exchange_id: str | None,
    ) -> None:
        draft = self._drafts.get(exchange_id)
        self._typing.stop(exchange_id)
        draft.pending_since = None
        self._delivery.cancel_timer(draft)
        if isinstance(event, Cancelled):
            _append_line(draft, CANCELLED_LINE)
        elif isinstance(event, Failed):
            _append_line(draft, FAILED_LINE_TEMPLATE.format(error=event.error))
        try:
            await self._flush_terminal(draft)
        finally:
            self._drafts.pop(exchange_id)
            await self._drafts.forget(exchange_id)

    async def _flush_terminal(self, draft: Draft) -> None:
        try:
            await self._delivery.flush(draft)
        except (TelegramApiError, httpx.HTTPError):
            await asyncio.sleep(TERMINAL_FLUSH_RETRY_DELAY_SECONDS)
            try:
                await self._delivery.flush(draft)
            except (TelegramApiError, httpx.HTTPError):
                logger.error(
                    "Telegram final flush failed twice for %s",
                    self._user_id,
                    exc_info=True,
                )


def _append_line(draft: Draft, line: str) -> None:
    if draft.buffer and not draft.buffer.endswith("\n"):
        draft.buffer += "\n"
    draft.buffer += line + "\n"
