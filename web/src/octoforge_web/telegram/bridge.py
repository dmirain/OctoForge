"""Bridge between a Telegram chat and the dialog runner: renders events, submits text."""

import asyncio
import logging
import time
from collections import OrderedDict
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
# how many sent-message-id -> exchange-id mappings the bridge remembers for
# reply routing; a restart loses the map entirely (routing falls back to the
# LLM router), so this only bounds in-process memory, not correctness
REPLY_TARGET_MAP_SIZE = 512
# a FINAL flush is the one delivery that must not be silently dropped: the
# client already retries 429s/transient failures internally, so a second
# bridge-level attempt after a short pause is for whatever still gets
# through (e.g. two independent transient failures back to back).
TERMINAL_FLUSH_RETRY_DELAY_SECONDS = 1.0


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
    # the exchange this draft belongs to, carried alongside so a fresh
    # message id can be recorded into the reply-target map at send time
    # without threading exchange_id through every render/flush call
    exchange_id: str | None = None


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
        # sent message id -> exchange id, for resolving a Telegram reply back
        # to its exchange without an LLM routing call; bounded (oldest
        # evicted first) and lost on restart, which just falls back to the
        # router — never a correctness issue, only a routing-cost one
        self._reply_targets: OrderedDict[int, str] = OrderedDict()

    async def start(self) -> None:
        """Resolve the runner and start forwarding its events to the chat."""
        if self._forwarder is not None and not self._forwarder.done():
            return
        runner = await self._ensure_runner()
        queue = runner.subscribe()  # subscribe before the run starts, events are not replayed
        self._forwarder = asyncio.create_task(self._forward(runner, queue))

    async def handle_text(
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        """Submit user text into the dialog, starting the forwarder on first contact.

        `client_message_id` (the chat-level Telegram message id) deduplicates
        delivery retries — Telegram re-sends an update until it gets a 200 —
        and doubles as the reply target the answer threads back to.
        `reply_to_message_id` is the message the user replied to, if any: when
        it names a message this bridge sent for a live exchange, routing
        skips the LLM router entirely (`ConversationRunner.submit`'s
        deterministic reply shortcut). An unknown or absent reply id falls
        back to the router, same as before.
        """
        runner = await self._ensure_runner()
        await self.start()
        await self._client.send_chat_action(self._chat_id, CHAT_ACTION_TYPING)
        reply_to_exchange_id = (
            self._reply_targets.get(reply_to_message_id)
            if reply_to_message_id is not None
            else None
        )
        await runner.submit(
            content,
            client_message_id=client_message_id,
            reply_to_exchange_id=reply_to_exchange_id,
        )

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
            draft = _Draft(exchange_id=exchange_id)
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
        try:
            await self._flush_terminal_with_retry(draft)
        finally:
            # the draft must never outlive its terminal event: a stale entry
            # left behind by a failed flush would corrupt (or silently eat)
            # the next answer delivered under the same exchange id
            self._drafts.pop(exchange_id, None)
        if isinstance(event, Finished):
            await self._upgrade_to_rich(draft)

    async def _flush_terminal_with_retry(self, draft: _Draft) -> None:
        """Flush the final draft, retrying once more on a transport/API hiccup.

        A FINAL flush that still fails after the retry is a message the user
        never sees; that is logged loudly (error, not warning) so it stands
        out from routine render hiccups.
        """
        try:
            await self._flush_draft(draft)
        except (TelegramApiError, httpx.HTTPError):
            logger.warning(
                "Telegram final flush failed for %s, retrying once", self._user_id, exc_info=True
            )
            await asyncio.sleep(TERMINAL_FLUSH_RETRY_DELAY_SECONDS)
            try:
                await self._flush_draft(draft)
            except (TelegramApiError, httpx.HTTPError):
                logger.error(
                    "Telegram final flush failed twice for %s; the answer was lost",
                    self._user_id,
                    exc_info=True,
                )

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
            if draft.exchange_id is not None:
                self._record_reply_target(draft.message_id, draft.exchange_id)
        else:
            await self._client.edit_message_text(
                self._chat_id, draft.message_id, html, parse_mode=PARSE_MODE_HTML
            )
        draft.delivered_text = html

    def _record_reply_target(self, message_id: int, exchange_id: str) -> None:
        """Remember a sent message as a reply target for its exchange (bounded)."""
        self._reply_targets[message_id] = exchange_id
        if len(self._reply_targets) > REPLY_TARGET_MAP_SIZE:
            self._reply_targets.popitem(last=False)  # evict the oldest mapping


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
    # assistant messages, either streamed live per-exchange or delivered whole
    # through the outbox — there is no separate foreground path anymore.
    return None
