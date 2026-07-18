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

logger = logging.getLogger(__name__)

RunnerProvider = Callable[[str, str], Awaitable[ConversationRunner]]

TOOL_LINE_TEMPLATE = "⚙️ {name}"
TOOL_FAIL_LINE_TEMPLATE = "⚠️ {name}: {error}"
SUSPENDED_LINE_TEMPLATE = "⏸️ «{title}» ушёл в фон"
RESUMED_LINE_TEMPLATE = "▶️ «{title}» снова активен"
CANCELLED_LINE = "🛑 Отменено"
FAILED_LINE_TEMPLATE = "❌ Ошибка: {error}"
MIN_BOUNDARY_RATIO = 2


@dataclass(slots=True)
class _Draft:
    """The one message being rendered for the current run of the chat."""

    message_id: int | None = None
    buffer: str = ""
    delivered_text: str = ""


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

    async def handle_text(self, content: str) -> None:
        """Submit user text into the dialog, starting the forwarder on first contact."""
        runner = await self._ensure_runner()
        await self.start()
        await self._client.send_chat_action(self._chat_id, CHAT_ACTION_TYPING)
        await runner.submit(content)

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
        text = self._draft.buffer.rstrip("\n")
        while len(text) > MAX_MESSAGE_LENGTH:
            head, text = split_head(text)
            await self._deliver(head)
            self._draft = _Draft(buffer=text)  # seal the head, continue in a fresh message
        if text and text != self._draft.delivered_text:
            await self._deliver(text)

    async def _deliver(self, text: str) -> None:
        if self._draft.message_id is None:
            self._draft.message_id = await self._client.send_message(self._chat_id, text)
        else:
            await self._client.edit_message_text(self._chat_id, self._draft.message_id, text)
        self._draft.delivered_text = text


def _status_line(event: LoopEvent) -> str | None:
    if isinstance(event, ToolCallRequested):
        return TOOL_LINE_TEMPLATE.format(name=event.call.name)
    if isinstance(event, ToolCallFailed):
        return TOOL_FAIL_LINE_TEMPLATE.format(name=event.call.name, error=event.error)
    if isinstance(event, ProcessSuspended):
        return SUSPENDED_LINE_TEMPLATE.format(title=event.title)
    if isinstance(event, ProcessResumed):
        return RESUMED_LINE_TEMPLATE.format(title=event.title)
    # ProcessCompleted is not rendered: completions already arrive as report-run text.
    return None


def split_head(text: str, limit: int = MAX_MESSAGE_LENGTH) -> tuple[str, str]:
    """Cut `text` into a Telegram-sized head chunk and the remaining tail."""
    cut = _find_cut(text, limit)
    return text[:cut], _drop_boundary(text[cut:])


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split `text` into Telegram-sized chunks, preferring line/word boundaries."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        head, remaining = split_head(remaining, limit)
        chunks.append(head)
    chunks.append(remaining)
    return chunks


def _find_cut(text: str, limit: int) -> int:
    for separator in ("\n", " "):
        cut = text.rfind(separator, 0, limit)
        if cut >= limit // MIN_BOUNDARY_RATIO:
            return cut
    return limit


def _drop_boundary(text: str) -> str:
    if text[:1] in ("\n", " "):
        return text[1:]
    return text
