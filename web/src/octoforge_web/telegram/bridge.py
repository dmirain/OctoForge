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
    ProcessStarted,
    RetryScheduled,
    TextDelta,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.agent.runner import ConversationEvent, ConversationRunner

from octoforge_web.telegram.client import (
    CHAT_ACTION_TYPING,
    MAX_MESSAGE_LENGTH,
    MAX_RICH_MESSAGE_LENGTH,
    TELEGRAM_CHANNEL,
    TelegramApiError,
    TelegramClient,
)
from octoforge_web.telegram.markdown import markdown_to_telegram_html, split_html_safe
from octoforge_web.telegram.rich import needs_rich_message

logger = logging.getLogger(__name__)

RunnerProvider = Callable[[str, str], Awaitable[ConversationRunner]]

TOOL_LINE_TEMPLATE = "⚙️ {name}"
TOOL_FAIL_LINE_TEMPLATE = "⚠️ {name}: {error}"
CANCELLED_LINE = "🛑 Отменено"
FAILED_LINE_TEMPLATE = "❌ Ошибка: {error}"
RETRY_LINE_TEMPLATE = "🔁 Провайдер недоступен ({reason}), повтор {attempt} через {delay:.0f} сек"
PARSE_MODE_HTML = "HTML"


@dataclass(slots=True)
class TelegramBridgeOptions:
    """Rendering knobs shared by every bridge of the surface."""

    edit_throttle_seconds: float
    rich_messages_enabled: bool = True


@dataclass(slots=True)
class _Draft:
    """One message being rendered for one exchange of the chat.

    There is one draft per exchange (plus one keyed None for broker notices
    and RUN-task results): answers of different questions stream
    concurrently, each into its own Telegram message.
    """

    message_id: int | None = None
    buffer: str = ""
    delivered_text: str = ""
    sealed_chunks: int = 0
    # the question this draft replies to (its chat-level message id); set
    # from ProcessStarted BEFORE the first send — Telegram can only thread
    # a reply at message creation, never on a later edit
    reply_to: int | None = None
    last_flush_monotonic: float = 0.0


class TelegramBridge:
    """One private chat bound to its dialog: events out to Telegram, user text in."""

    def __init__(
        self,
        user_id: str,
        chat_id: int,
        runner_provider: RunnerProvider,
        client: TelegramClient,
        options: TelegramBridgeOptions,
    ) -> None:
        self._user_id = user_id
        self._chat_id = chat_id
        self._runner_provider = runner_provider
        self._client = client
        self._edit_throttle_seconds = options.edit_throttle_seconds
        self._rich_messages_enabled = options.rich_messages_enabled
        self._runner: ConversationRunner | None = None
        self._forwarder: asyncio.Task[None] | None = None
        self._drafts: dict[str | None, _Draft] = {}

    async def start(self) -> None:
        """Resolve the runner and start forwarding its events to the chat."""
        if self._forwarder is not None and not self._forwarder.done():
            return
        runner = await self._ensure_runner()
        queue = runner.subscribe()  # subscribe before the run starts, events are not replayed
        self._forwarder = asyncio.create_task(self._forward(runner, queue))

    async def handle_text(self, content: str, client_message_id: str | None = None) -> None:
        """Submit user text into the dialog, starting the forwarder on first contact.

        `client_message_id` (the chat-level Telegram message id) deduplicates
        delivery retries — Telegram re-sends an update until it gets a 200 —
        and doubles as the reply target the answer threads back to.
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
                await self._render_safely(event.payload, event.exchange_id)
        finally:
            runner.unsubscribe(queue)

    async def _render_safely(self, event: LoopEvent, exchange_id: str | None) -> None:
        try:
            await self._render(event, exchange_id)
        except (TelegramApiError, httpx.HTTPError):
            logger.warning("Telegram render failed for %s", self._user_id, exc_info=True)

    def _draft_of(self, exchange_id: str | None) -> _Draft:
        draft = self._drafts.get(exchange_id)
        if draft is None:
            draft = _Draft()
            self._drafts[exchange_id] = draft
        return draft

    async def _render(self, event: LoopEvent, exchange_id: str | None) -> None:
        draft = self._draft_of(exchange_id)
        if isinstance(event, ProcessStarted):
            if draft.message_id is None:
                draft.reply_to = _reply_target(event.source_client_message_id)
            return
        if isinstance(event, TextDelta):
            draft.buffer += event.text
            await self._flush_throttled(draft)
            return
        if isinstance(event, (Finished, Cancelled, Failed)):
            await self._render_terminal(event, exchange_id, draft)
            return
        line = _status_line(event)
        if line is not None:
            self._append_line(draft, line)
            await self._flush_throttled(draft)

    async def _render_terminal(
        self, event: Finished | Cancelled | Failed, exchange_id: str | None, draft: _Draft
    ) -> None:
        if isinstance(event, Cancelled):
            self._append_line(draft, CANCELLED_LINE)
        elif isinstance(event, Failed):
            self._append_line(draft, FAILED_LINE_TEMPLATE.format(error=event.error))
        await self._flush_draft(draft)
        if isinstance(event, Finished):
            await self._upgrade_to_rich(draft)
        self._drafts.pop(exchange_id, None)

    async def _upgrade_to_rich(self, draft: _Draft) -> None:
        """Re-render the final answer as a native Rich Message when it earns one.

        The draft stays on the legacy HTML path; only a single-message final
        with constructs the HTML rendering degrades is upgraded in place.
        A failed upgrade leaves the HTML version on screen (logged upstream).
        """
        raw = draft.buffer.rstrip("\n")
        if (
            not self._rich_messages_enabled
            or draft.message_id is None
            or draft.sealed_chunks > 0
            or not raw
            or len(raw) > MAX_RICH_MESSAGE_LENGTH
            or not needs_rich_message(raw)
        ):
            return
        await self._client.edit_message_rich(self._chat_id, draft.message_id, raw)

    def _append_line(self, draft: _Draft, line: str) -> None:
        """Append a status line, keeping the arrival order with the answer text."""
        if draft.buffer and not draft.buffer.endswith("\n"):
            draft.buffer += "\n"
        draft.buffer += line + "\n"

    async def _flush_throttled(self, draft: _Draft) -> None:
        now = time.monotonic()
        if now - draft.last_flush_monotonic >= self._edit_throttle_seconds:
            draft.last_flush_monotonic = now
            await self._flush_draft(draft)

    async def _flush_draft(self, draft: _Draft) -> None:
        raw = draft.buffer.rstrip("\n")
        if not raw:
            return
        # The buffer holds raw Markdown; the 4096 limit applies to its HTML form.
        chunks = split_html_safe(markdown_to_telegram_html(raw), MAX_MESSAGE_LENGTH)
        while draft.sealed_chunks < len(chunks) - 1:
            await self._deliver(draft, chunks[draft.sealed_chunks])
            draft.message_id = None  # seal the head, continue in a fresh message
            draft.delivered_text = ""
            draft.sealed_chunks += 1
        if chunks[-1] != draft.delivered_text:
            await self._deliver(draft, chunks[-1])

    async def _deliver(self, draft: _Draft, html: str) -> None:
        if draft.message_id is None:
            # only the head of the answer replies; continuation chunks of a
            # long answer (sealed_chunks > 0) are plain follow-ups
            reply_to = draft.reply_to if draft.sealed_chunks == 0 else None
            draft.message_id = await self._client.send_message(
                self._chat_id, html, parse_mode=PARSE_MODE_HTML, reply_to_message_id=reply_to
            )
        else:
            await self._client.edit_message_text(
                self._chat_id, draft.message_id, html, parse_mode=PARSE_MODE_HTML
            )
        draft.delivered_text = html


def _reply_target(source_client_message_id: str | None) -> int | None:
    """Parse the source key into a chat message id; non-numeric keys mean no reply.

    The poller keys Telegram submits by the chat-level message id, so a
    numeric key is a valid reply target. None (requeued messages, tasks
    created before the key existed) and non-numeric keys (other transports)
    simply fall back to a plain, unthreaded message.
    """
    if source_client_message_id is None or not source_client_message_id.isdigit():
        return None
    return int(source_client_message_id)


def _status_line(event: LoopEvent) -> str | None:
    if isinstance(event, ToolCallRequested):
        return TOOL_LINE_TEMPLATE.format(name=event.call.name)
    if isinstance(event, ToolCallFailed):
        return TOOL_FAIL_LINE_TEMPLATE.format(name=event.call.name, error=event.error)
    if isinstance(event, RetryScheduled):
        return RETRY_LINE_TEMPLATE.format(
            reason=event.reason, attempt=event.attempt, delay=event.delay_seconds
        )
    # ProcessCompleted is not rendered: completions arrive as directly delivered
    # assistant messages (foreground stream or broker outbox delivery).
    return None
