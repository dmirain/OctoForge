"""Bridge between a Telegram chat and the dialog runner: renders events, submits text."""

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

import httpx
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    LoopEvent,
    ProcessStarted,
    ReasoningDelta,
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
    ACTIVITY_INTERVAL_SECONDS,
    CHAT_ACTION_TYPING,
    MAX_RICH_MESSAGE_LENGTH,
    TELEGRAM_CHANNEL,
    TelegramApiError,
    TelegramClient,
)
from octoforge_telegram.drafts import DraftStore, PersistedDraft

logger = logging.getLogger(__name__)

RunnerProvider = Callable[[str, str], Awaitable[ConversationRunner]]

# the fallback for a tool outside every group: the raw name is still better
# than hiding that something runs
TOOL_LINE_TEMPLATE = "⚙️ {name}"
TOOL_FAIL_LINE_TEMPLATE = "⚠️ {name}: {error}"
CANCELLED_LINE = "🛑 Отменено"
FAILED_LINE_TEMPLATE = "❌ Ошибка: {error}"
RETRY_LINE_TEMPLATE = "🔁 Провайдер недоступен ({reason}), повтор {attempt} через {delay:.0f} сек"
# the label for a reasoning phase; a persistent entry like any other, its
# progress dots counting the reasoning chunks of that phase
THINKING_LABEL = "💭 думаю"
# what the user reads instead of raw tool names: the agent narrating its own
# actions — first-person verbs, one per tool. Consecutive calls of one kind
# collapse into a single entry with progress dots.
TOOL_GROUPS: dict[str, str] = {
    "recall": "🧠 вспоминаю",
    "memory_store": "🧠 запоминаю",
    "memory_delete": "🧠 забываю",
    "history_search": "💬 читаю историю",
    "web_search": "🔎 ищу",
    "http_request": "🌐 запрашиваю",
    "external_call": "🔌 вызываю",
    "endpoint_get": "🔌 вызываю",
    "image_look": "🖼 смотрю",
    "cron_pause": "⏰ планирую",
    "cron_resume": "⏰ планирую",
    "data_put": "📊 записываю",
    "data_query": "📊 выбираю",
    "data_forget": "📊 стираю",
    "instruction_save": "📚 сохраняю",
    "instruction_delete": "📚 удаляю",
    "task_create": "🗂 запускаю",
    "task_delete": "🗂 отменяю",
    "task_list": "🗂 сверяюсь",
    "ask_user": "❓ спрашиваю",
}
# separates entries within the single status line
STATUS_SEPARATOR = "  "
# progress dots grow one per call up to this; then the last dot becomes the
# count itself: · ·· ··· ··4 ··5 …
MAX_PLAIN_DOTS = 3
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
class _StatusEntry:
    """One line of the monospaced status block above the answer text.

    Counted entries (tool groups) collapse consecutive repeats into growing
    progress dots; notices (a failed call, a retry, a terminal state) stand
    as they are.
    """

    label: str
    count: int = 1
    counted: bool = True


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
    # when undelivered output STARTED accumulating; the flush fires one
    # throttle interval after this, so the first visible block is a real
    # block and not the stream's first two letters
    pending_since: float | None = None
    # delivers the pending buffer when no further event arrives to do it —
    # without it, a model pausing mid-answer left text sitting undelivered
    # until the pause ended
    flush_timer: asyncio.Task[None] | None = None
    # the status line above the answer: the agent's actions (thinking
    # included) with progress dots, plus notices, in arrival order
    status: list[_StatusEntry] = field(default_factory=list)
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
        # exchanges with an answer in flight; while non-empty, one pulse task
        # keeps the chat-level typing indicator alive (it expires after ~5s,
        # so a single action at submit time dies long before a slow answer)
        self._typing_exchanges: set[str | None] = set()
        self._typing_pulse: asyncio.Task[None] | None = None

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
            # instant feedback for the routing gap only; from ProcessStarted
            # on, the pulse task keeps the indicator alive for the whole run
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
        self._stop_typing()
        for draft in self._drafts.values():
            self._cancel_flush_timer(draft)
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
            # whoever owns the dialog next shows their own indicator and
            # continues the drafts; ours must not keep promising an answer
            # this process will not send, nor fire a late edit into a
            # message the new owner is already writing
            self._stop_typing()
            for draft in self._drafts.values():
                self._cancel_flush_timer(draft)
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
            self._typing_on(exchange_id)
            return
        if isinstance(event, ReasoningDelta):
            _count_status(draft, THINKING_LABEL)
            await self._flush_throttled(draft)
            return
        if isinstance(event, TextDelta):
            draft.buffer += event.text
            await self._flush_throttled(draft)
            return
        if isinstance(event, (Finished, Cancelled, Failed)):
            await self._render_terminal(event, exchange_id, draft)
            return
        if isinstance(event, ToolCallRequested):
            _count_status(draft, _tool_group(event.call.name))
            await self._flush_throttled(draft)
            return
        line = _notice_line(event)
        if line is not None:
            draft.status.append(_StatusEntry(label=line, counted=False))
            await self._flush_throttled(draft)

    async def _render_terminal(
        self, event: Finished | Cancelled | Failed, exchange_id: str | None, draft: _Draft
    ) -> None:
        # stop pulsing before the final flush, so the message that ends the
        # answer is also the thing that clears the indicator — a pulse after
        # it would show "typing" with nothing left to type. Same for the
        # deferred flush timer: the terminal flush below is the last word,
        # nothing may fire after it.
        self._typing_off(exchange_id)
        draft.pending_since = None
        self._cancel_flush_timer(draft)
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
        """Append a terminal line to the answer text (Отменено / Ошибка).

        Outcomes belong under the text they conclude, not in the status
        block above it.
        """
        if draft.buffer and not draft.buffer.endswith("\n"):
            draft.buffer += "\n"
        draft.buffer += line + "\n"

    async def _flush_throttled(self, draft: _Draft) -> None:
        """Deliver one block per throttle window, not one edit per delta.

        The clock starts when undelivered output starts ACCUMULATING, so the
        very first flush also waits a full window — the answer opens with a
        readable block instead of the stream's first two letters. The timer
        covers the other direction: output already pending is delivered on
        time even when the stream goes silent (a model pausing mid-answer
        used to leave its last words sitting in the buffer until the pause
        ended).
        """
        now = time.monotonic()
        if draft.pending_since is None:
            draft.pending_since = now
        await self._flush_when_due(draft, now)

    async def _flush_when_due(self, draft: _Draft, now: float) -> None:
        if draft.pending_since is None:
            return
        wait = draft.pending_since + self._edit_throttle_seconds - now
        if wait <= 0:
            # reset BEFORE the await: a concurrent entry (the timer firing
            # during this flush's HTTP call) must see nothing left to do
            draft.pending_since = None
            await self._flush_draft(draft)
        elif draft.flush_timer is None or draft.flush_timer.done():
            draft.flush_timer = asyncio.create_task(self._flush_after(draft, wait))

    async def _flush_after(self, draft: _Draft, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._flush_when_due(draft, time.monotonic())
        except (TelegramApiError, httpx.HTTPError):
            logger.warning("Telegram deferred flush failed for %s", self._user_id, exc_info=True)

    @staticmethod
    def _cancel_flush_timer(draft: _Draft) -> None:
        if draft.flush_timer is not None:
            draft.flush_timer.cancel()
            draft.flush_timer = None

    async def _flush_draft(self, draft: _Draft) -> None:
        """Push the draft to the chat as the Rich Message(s) it renders into.

        The visible message is composed on every flush: one monospaced
        status line (the agent's actions with progress dots, thinking
        included), then the answer text — the agent's Markdown, which
        Telegram renders natively. The limit that applies is the Rich
        Message one, eight times the plain-text budget; an answer that
        still outgrows it is sealed and continued in a fresh message.
        """
        raw = _compose(draft)
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

    def _typing_on(self, exchange_id: str | None) -> None:
        """An answer for this exchange is in flight: keep "typing" alive.

        One pulse task serves however many exchanges are running — the
        indicator is per chat, not per message, so there is nothing to gain
        from more than one.
        """
        self._typing_exchanges.add(exchange_id)
        if self._typing_pulse is None or self._typing_pulse.done():
            self._typing_pulse = asyncio.create_task(self._pulse_typing())

    def _typing_off(self, exchange_id: str | None) -> None:
        self._typing_exchanges.discard(exchange_id)
        if not self._typing_exchanges and self._typing_pulse is not None:
            self._typing_pulse.cancel()
            self._typing_pulse = None

    def _stop_typing(self) -> None:
        """Drop every in-flight exchange (shutdown or stand-down)."""
        self._typing_exchanges.clear()
        if self._typing_pulse is not None:
            self._typing_pulse.cancel()
            self._typing_pulse = None

    async def _pulse_typing(self) -> None:
        """Re-send the chat action until cancelled; a failed indicator is not fatal."""
        while True:
            with suppress(TelegramApiError, httpx.HTTPError):
                await self._client.send_chat_action(self._chat_id, CHAT_ACTION_TYPING)
            await asyncio.sleep(ACTIVITY_INTERVAL_SECONDS)

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


def _notice_line(event: LoopEvent) -> str | None:
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


def _tool_group(name: str) -> str:
    """The user-facing label of a tool: its group's emoji + short Russian word."""
    return TOOL_GROUPS.get(name) or TOOL_LINE_TEMPLATE.format(name=name)


def _count_status(draft: _Draft, label: str) -> None:
    """Add a counted status entry; a back-to-back repeat only grows the dots."""
    last = draft.status[-1] if draft.status else None
    if last is not None and last.counted and last.label == label:
        last.count += 1
        return
    draft.status.append(_StatusEntry(label=label))


def _progress_dots(count: int) -> str:
    """· then ·· then ···, and past that the last dot becomes the count."""
    if count <= MAX_PLAIN_DOTS:
        return "·" * count
    return f"··{count}"


def _compose(draft: _Draft) -> str:
    """The full visible message: one monospaced status line, then the text.

    The agent narrates what it does in a single line — entries flow one
    after another, never one per row — and the record stays in the finished
    message: thinking phases are history, not a spinner.
    """
    entries = [
        f"{entry.label} {_progress_dots(entry.count)}" if entry.counted else entry.label
        for entry in draft.status
    ]
    text = draft.buffer.rstrip("\n")
    parts: list[str] = []
    if entries:
        parts.append("```\n" + STATUS_SEPARATOR.join(entries) + "\n```")
    if text:
        parts.append(text)
    return "\n".join(parts)
