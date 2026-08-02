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
from octoforge_core.agent.runner import (
    STREAM_CLOSED,
    ConversationRunner,
    SubscriberQueue,
)
from octoforge_core.domain import Attachment, MessageKind, MessageSource

from octoforge_telegram.client import (
    CHAT_ACTION_TYPING,
    MAX_RICH_MESSAGE_LENGTH,
    TELEGRAM_CHANNEL,
    TelegramApiError,
    TelegramClient,
)
from octoforge_telegram.drafts import DraftStore, PersistedDraft

logger = logging.getLogger(__name__)

RunnerProvider = Callable[[str, str], Awaitable[ConversationRunner]]

TOOL_LINE_TEMPLATE = "⚙️ {name}"
TOOL_FAIL_LINE_TEMPLATE = "⚠️ {name}: {error}"
CANCELLED_LINE = "🛑 Отменено"
FAILED_LINE_TEMPLATE = "❌ Ошибка: {error}"
RETRY_LINE_TEMPLATE = "🔁 Провайдер недоступен ({reason}), повтор {attempt} через {delay:.0f} сек"
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
    # remembers which message each live answer is being written into, so a
    # dialog that moves to another process keeps editing it instead of
    # starting a second one. None keeps drafts in memory only.
    drafts: DraftStore | None = None


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
        self._drafts_store = options.drafts
        self._runner: ConversationRunner | None = None
        self._forwarder: asyncio.Task[None] | None = None
        # Raised for the whole of `start()`, not just around its awaits: see
        # the note there. `_forwarder` alone cannot express "starting".
        self._starting = False
        self._drafts: dict[str | None, _Draft] = {}
        # sent message id -> exchange id, for resolving a Telegram reply back
        # to its exchange without an LLM routing call; bounded (oldest
        # evicted first) and lost on restart, which just falls back to the
        # router — never a correctness issue, only a routing-cost one
        self._reply_targets: OrderedDict[int, str] = OrderedDict()

    async def start(self) -> None:
        """Resolve the runner and start forwarding its events to the chat.

        Remembered drafts are restored first: this bridge may be picking up a
        dialog another process was mid-answer on, and the answer has to
        continue in the message the user is already looking at.

        Re-entrant. Resolving the runner attaches this surface to it, and
        attaching means starting the bridge for that dialog — which is this
        method, called on this very object, from inside its own
        `_ensure_runner()`. Guarding on `_forwarder` alone cannot see that:
        it is still None until the last line, so the nested call ran the
        whole body, and the outer one then subscribed a SECOND time. Both
        forwarders appended deltas to one draft, and the user read every
        answer twice, interleaved.

        The flag is raised before the first await, so the nested call returns
        at once and lets the outer finish the job. It also makes two
        concurrent starts safe, which the old check was not.
        """
        if self._starting:
            return
        if self._forwarder is not None and not self._forwarder.done():
            return
        self._starting = True
        try:
            await self._restore_drafts()
            runner = await self._ensure_runner()
            # subscribe before the run starts: events are not replayed
            queue = runner.subscribe()
            self._forwarder = asyncio.create_task(self._forward(runner, queue))
        finally:
            self._starting = False

    async def handle_text(  # noqa: PLR0913, PLR0917 — transport-shaped boundary signature
        self,
        content: str,
        client_message_id: str | None = None,
        reply_to_message_id: int | None = None,
        kind: MessageKind = MessageKind.OWN,
        origin: str | None = None,
        attachments: tuple[Attachment, ...] = (),
    ) -> None:
        """Submit user text into the dialog, starting the forwarder on first contact.

        `client_message_id` (the chat-level Telegram message id) deduplicates
        delivery retries — Telegram re-sends an update until it gets a 200 —
        and doubles as the reply target the answer threads back to.
        `reply_to_message_id` is the message the user replied to, if any: when
        it names a message this bridge sent for a live exchange, routing
        skips the LLM router entirely (`ConversationRunner.submit`'s
        deterministic reply shortcut). An unknown or absent reply id falls
        back to the router, same as before. `kind=MATERIAL` marks forwarded
        content: no answer follows it directly, so the typing indicator is
        skipped — it would promise a reply that is not coming yet.
        `attachments` carries transport-scoped references (e.g. an ingested
        Telegram photo) alongside the already-described text; empty for a
        plain text message.
        """
        runner = await self._ensure_runner()
        await self.start()
        if kind is MessageKind.OWN:
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
            source=MessageSource(kind=kind, origin=origin, attachments=attachments),
        )

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

    async def _forward(self, runner: ConversationRunner, queue: SubscriberQueue) -> None:
        """Render the runner's events until it stops or stands down.

        A stand-down means another process owns this dialog now. The cached
        runner is dropped rather than resubscribed to: re-resolving here would
        claim the dialog straight back from whoever just took it, and the two
        processes would trade it forever. The next incoming message rebuilds
        the binding through the normal path, which is also the moment the
        answer has somewhere to go.
        """
        try:
            while True:
                event = await queue.get()
                if event is STREAM_CLOSED:
                    logger.info("dialog moved to another owner: user=%s", self._user_id)
                    self._runner = None
                    return
                await self._render_safely(event.payload, event.exchange_id)
        finally:
            runner.unsubscribe(queue)

    async def _render_safely(self, event: LoopEvent, exchange_id: str | None) -> None:
        try:
            await self._render(event, exchange_id)
        except (TelegramApiError, httpx.HTTPError):
            logger.warning("Telegram render failed for %s", self._user_id, exc_info=True)

    async def _restore_drafts(self) -> None:
        """Adopt the messages a previous owner of this dialog was writing into."""
        if self._drafts_store is None:
            return
        try:
            remembered = await self._drafts_store.load(self._chat_id)
        except Exception:  # a lost draft costs a duplicate message, not the answer
            logger.warning("could not load drafts for %s", self._user_id, exc_info=True)
            return
        for item in remembered:
            self._drafts.setdefault(
                item.exchange_id,
                _Draft(
                    exchange_id=item.exchange_id,
                    message_id=item.message_id,
                    reply_to=item.reply_to,
                    sealed_chunks=item.sealed_chunks,
                ),
            )

    async def _remember_draft(self, draft: _Draft) -> None:
        """Write down which message this exchange's answer is being written into.

        Only when a message is CREATED — an edit that appends text changes
        nothing another process would need. Drafts with no exchange (broker
        notices, RUN results) are one-shot and not remembered.
        """
        if self._drafts_store is None or draft.exchange_id is None or draft.message_id is None:
            return
        try:
            await self._drafts_store.remember(
                self._chat_id,
                PersistedDraft(
                    exchange_id=draft.exchange_id,
                    message_id=draft.message_id,
                    reply_to=draft.reply_to,
                    sealed_chunks=draft.sealed_chunks,
                ),
            )
        except Exception:  # rendering must not fail because bookkeeping did
            logger.warning("could not remember draft for %s", self._user_id, exc_info=True)

    async def _forget_draft(self, exchange_id: str | None) -> None:
        """Drop the remembered draft; this answer is final."""
        if self._drafts_store is None or exchange_id is None:
            return
        try:
            await self._drafts_store.forget(exchange_id)
        except Exception:
            logger.warning("could not forget draft for %s", self._user_id, exc_info=True)

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
            await self._forget_draft(exchange_id)

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
        """Push the buffer to the chat as the Rich Message(s) it renders into.

        The buffer is the agent's Markdown and Telegram renders it natively,
        so nothing is converted on the way out — the limit that applies is
        the Rich Message one, eight times the plain-text budget. An answer
        that still outgrows it is sealed and continued in a fresh message.
        """
        raw = draft.buffer.rstrip("\n")
        if not raw:
            return
        chunks = _split_markdown(raw, MAX_RICH_MESSAGE_LENGTH)
        while draft.sealed_chunks < len(chunks) - 1:
            if chunks[draft.sealed_chunks] != draft.delivered_text:
                await self._deliver(draft, chunks[draft.sealed_chunks])
            draft.message_id = None  # seal the head, continue in a fresh message
            draft.delivered_text = ""
            draft.sealed_chunks += 1
        if chunks[-1] != draft.delivered_text:
            await self._deliver(draft, chunks[-1])

    async def _deliver(self, draft: _Draft, markdown: str) -> None:
        if draft.message_id is None:
            # only the head of the answer replies; continuation chunks of a
            # long answer (sealed_chunks > 0) are plain follow-ups
            reply_to = draft.reply_to if draft.sealed_chunks == 0 else None
            draft.message_id = await self._client.send_rich_message(
                self._chat_id, markdown, reply_to_message_id=reply_to
            )
            if draft.exchange_id is not None:
                self._record_reply_target(draft.message_id, draft.exchange_id)
            await self._remember_draft(draft)
        else:
            await self._client.edit_message_rich(self._chat_id, draft.message_id, markdown)
        draft.delivered_text = markdown

    def _record_reply_target(self, message_id: int, exchange_id: str) -> None:
        """Remember a sent message as a reply target for its exchange (bounded)."""
        self._reply_targets[message_id] = exchange_id
        if len(self._reply_targets) > REPLY_TARGET_MAP_SIZE:
            self._reply_targets.popitem(last=False)  # evict the oldest mapping


def _split_markdown(text: str, limit: int) -> list[str]:
    """Split Markdown into chunks of at most `limit` characters.

    Cuts land on line boundaries, so a table row, a list item or a fenced
    line is never torn in half — the coarsest thing a cut can break is a
    block, and only for an answer past `MAX_RICH_MESSAGE_LENGTH`, which no
    real one reaches. A single line longer than the limit (a pasted blob
    with no newline in it) is cut hard: there is nothing better to cut on.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        rest = line
        while len(rest) > limit:  # a single line past the limit: cut it hard
            if current:
                chunks.append(current)
                current = ""
            chunks.append(rest[:limit])
            rest = rest[limit:]
        if len(current) + len(rest) > limit:
            chunks.append(current)
            current = ""
        current += rest
    if current:
        chunks.append(current)
    # a cut lands on a line break, so the piece after it opens with the
    # newline it was cut on: trim both ends rather than start a message blank
    trimmed = [stripped for chunk in chunks if (stripped := chunk.strip("\n"))]
    return trimmed or [text[:limit]]


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
