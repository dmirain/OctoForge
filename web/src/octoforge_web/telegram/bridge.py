"""Bridge between a Telegram chat and the dialog runner: renders events, submits text."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

import httpx
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    LoopEvent,
    ProcessResumed,
    ProcessSuspended,
    RetryScheduled,
    TextDelta,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.agent.runner import ConversationEvent, ConversationRunner

from octoforge_web.telegram.client import (
    CHAT_ACTION_TYPING,
    MAX_MESSAGE_LENGTH,
    TELEGRAM_CHANNEL,
    TelegramApiError,
    TelegramClient,
)
from octoforge_web.telegram.markdown import markdown_to_telegram_html, split_html_safe

logger = logging.getLogger(__name__)

RunnerProvider = Callable[[str, str], Awaitable[ConversationRunner]]

TOOL_LINE_TEMPLATE = "⚙️ {name}"
TOOL_FAIL_LINE_TEMPLATE = "⚠️ {name}: {error}"
SUSPENDED_LINE_TEMPLATE = "⏸️ «{title}» ушёл в фон"
RESUMED_LINE_TEMPLATE = "▶️ «{title}» снова активен"
CANCELLED_LINE = "🛑 Отменено"
FAILED_LINE_TEMPLATE = "❌ Ошибка: {error}"
RETRY_LINE_TEMPLATE = "🔁 Провайдер недоступен ({reason}), повтор {attempt} через {delay:.0f} сек"
PARSE_MODE_HTML = "HTML"


@dataclass(slots=True)
class _Draft:
    """The one message being rendered for the current run of the chat."""

    message_id: int | None = None
    buffer: str = ""
    delivered_text: str = ""
    sealed_chunks: int = 0


class TelegramBridge:
    """One private chat bound to its dialog: events out to Telegram, user text in."""

    def __init__(
        self,
        user_id: str,
        chat_id: int,
        runner_provider: RunnerProvider,
        client: TelegramClient,
        edit_throttle_seconds: float,
    ) -> None:
        self._user_id = user_id
        self._chat_id = chat_id
        self._runner_provider = runner_provider
        self._client = client
        self._edit_throttle_seconds = edit_throttle_seconds
        self._runner: ConversationRunner | None = None
        self._forwarder: asyncio.Task[None] | None = None
        self._draft = _Draft()
        self._last_flush_monotonic = 0.0

    async def start(self) -> None:
        """Resolve the runner and start forwarding its events to the chat."""
        if self._forwarder is not None and not self._forwarder.done():
            return
        runner = await self._ensure_runner()
        queue = runner.subscribe()  # subscribe before the run starts, events are not replayed
        self._forwarder = asyncio.create_task(self._forward(runner, queue))

    async def handle_text(self, content: str, client_message_id: str | None = None) -> None:
        """Submit user text into the dialog, starting the forwarder on first contact.

        `client_message_id` (Telegram update_id) deduplicates delivery
        retries: Telegram re-sends an update until it gets a 200.
        """
        runner = await self._ensure_runner()
        await self.start()
        await self._client.send_chat_action(self._chat_id, CHAT_ACTION_TYPING)
        await runner.submit(content, client_message_id=client_message_id)

    async def cancel(self) -> None:
        """Cancel the current run of the dialog."""
        runner = await self._ensure_runner()
        await runner.cancel()

    async def aclose(self) -> None:
        """Stop forwarding events (on app shutdown)."""
        if self._forwarder is not None:
            self._forwarder.cancel()
            with suppress(asyncio.CancelledError):
                await self._forwarder

    async def _ensure_runner(self) -> ConversationRunner:
        if self._runner is None:
            self._runner = await self._runner_provider(self._user_id, TELEGRAM_CHANNEL)
        return self._runner

    async def _forward(
        self, runner: ConversationRunner, queue: asyncio.Queue[ConversationEvent]
    ) -> None:
        try:
            while True:
                event = await queue.get()
                await self._render_safely(event.payload)
        finally:
            runner.unsubscribe(queue)

    async def _render_safely(self, event: LoopEvent) -> None:
        try:
            await self._render(event)
        except (TelegramApiError, httpx.HTTPError):
            logger.warning("Telegram render failed for %s", self._user_id, exc_info=True)

    async def _render(self, event: LoopEvent) -> None:
        if isinstance(event, TextDelta):
            self._draft.buffer += event.text
            await self._flush_throttled()
            return
        if isinstance(event, (Finished, Cancelled, Failed)):
            await self._render_terminal(event)
            return
        line = _status_line(event)
        if line is not None:
            self._append_line(line)
            await self._flush_throttled()

    async def _render_terminal(self, event: Finished | Cancelled | Failed) -> None:
        if isinstance(event, Cancelled):
            self._append_line(CANCELLED_LINE)
        elif isinstance(event, Failed):
            self._append_line(FAILED_LINE_TEMPLATE.format(error=event.error))
        await self._flush_draft()
        self._draft = _Draft()

    def _append_line(self, line: str) -> None:
        """Append a status line, keeping the arrival order with the answer text."""
        if self._draft.buffer and not self._draft.buffer.endswith("\n"):
            self._draft.buffer += "\n"
        self._draft.buffer += line + "\n"

    async def _flush_throttled(self) -> None:
        now = time.monotonic()
        if now - self._last_flush_monotonic >= self._edit_throttle_seconds:
            self._last_flush_monotonic = now
            await self._flush_draft()

    async def _flush_draft(self) -> None:
        raw = self._draft.buffer.rstrip("\n")
        if not raw:
            return
        # The buffer holds raw Markdown; the 4096 limit applies to its HTML form.
        chunks = split_html_safe(markdown_to_telegram_html(raw), MAX_MESSAGE_LENGTH)
        while self._draft.sealed_chunks < len(chunks) - 1:
            await self._deliver(chunks[self._draft.sealed_chunks])
            self._draft.message_id = None  # seal the head, continue in a fresh message
            self._draft.delivered_text = ""
            self._draft.sealed_chunks += 1
        if chunks[-1] != self._draft.delivered_text:
            await self._deliver(chunks[-1])

    async def _deliver(self, html: str) -> None:
        if self._draft.message_id is None:
            self._draft.message_id = await self._client.send_message(
                self._chat_id, html, parse_mode=PARSE_MODE_HTML
            )
        else:
            await self._client.edit_message_text(
                self._chat_id, self._draft.message_id, html, parse_mode=PARSE_MODE_HTML
            )
        self._draft.delivered_text = html


def _status_line(event: LoopEvent) -> str | None:
    if isinstance(event, ToolCallRequested):
        return TOOL_LINE_TEMPLATE.format(name=event.call.name)
    if isinstance(event, ToolCallFailed):
        return TOOL_FAIL_LINE_TEMPLATE.format(name=event.call.name, error=event.error)
    if isinstance(event, ProcessSuspended):
        return SUSPENDED_LINE_TEMPLATE.format(title=event.title)
    if isinstance(event, ProcessResumed):
        return RESUMED_LINE_TEMPLATE.format(title=event.title)
    if isinstance(event, RetryScheduled):
        return RETRY_LINE_TEMPLATE.format(
            reason=event.reason, attempt=event.attempt, delay=event.delay_seconds
        )
    # ProcessCompleted is not rendered: completions already arrive as report-run text.
    return None
