"""Default ContextCompactor: hot-tail policy and background LLM summarization.

Assemble returns `[topics block?] + hot tail`: one system message with the
dialog's rolling summary (skipped when there is none), then the archive tail
with `seq > max(seq_to)` taken from the actor's narrative (its in-memory
mirror). When the tail outgrows the configured limit, a background asyncio
task compresses the oldest tail messages, merging them into the single
rolling summary — one LLM call, one store write; the fresh tail is never
touched. Compaction is technical work, not an actor process: it stays
invisible to the dialog, guarded to one run per dialog, and a failure is a
logged warning, never a dialog error. `compact_now` runs the same merge
synchronously — the reactive path after a provider context overflow.
"""

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.context.api import (
    INTERRUPTED_NOTE,
    NO_COMPACTED_SEQ,
    ArchivedMessage,
    AssembledContext,
    ContextCompactor,
    DialogueSummary,
    MessageArchive,
    SummaryStore,
)
from octoforge_core.context.prompts import (
    SUMMARY_SYSTEM_PROMPT,
    format_merge_request,
    parse_summary_reply,
)
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.ports import LLMClient
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

DEFAULT_HOT_MAX_CHARS = 12000
DEFAULT_COMPACT_TARGET_CHARS = 6000
DEFAULT_MODEL_CONTEXT_TOKENS = 0  # 0 = token trigger disabled
DEFAULT_CONTEXT_BUFFER_TOKENS = 2000
TOPICS_BLOCK_HEADER = (
    "Compressed summaries of earlier topics of this conversation "
    "(the verbatim recent history follows):"
)
TOPIC_ENTRY_TEMPLATE = "[seq {seq_from}-{seq_to}] (topics: {topics}) {content}"
NO_TOPICS = "-"
# One compaction run consumes at most compact_target_chars from the head of
# the tail, so reading more rows than could ever fill a segment is waste — and
# on a long-uncompacted dialog (compaction disabled or broken for a while) an
# unbounded read would pull the entire backlog into memory and spend the
# event loop converting it. Generous vs a normal segment (~tens of messages);
# with a partial window the keep-last rule doubles as the pair-split guard at
# the window edge, and the next run continues from the advanced boundary.
SEGMENT_FETCH_LIMIT = 500


@dataclass(frozen=True, slots=True)
class CompactorConfig:
    """Limits of the hot-tail policy.

    Chars are a ~4:1 proxy of tokens. When `model_context_tokens` is set
    and the provider reports usage, the token trigger (last run's
    prompt_tokens >= model_context - buffer) complements the chars heuristic.
    """

    hot_max_chars: int = DEFAULT_HOT_MAX_CHARS
    compact_target_chars: int = DEFAULT_COMPACT_TARGET_CHARS
    model_context_tokens: int = DEFAULT_MODEL_CONTEXT_TOKENS
    context_buffer_tokens: int = DEFAULT_CONTEXT_BUFFER_TOKENS


class NoopContextCompactor(ContextCompactor):
    """ContextCompactor passthrough: the full narrative, never any compaction."""

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        """Return the history unchanged (everything is the hot tail)."""
        return AssembledContext(messages=list(history), tail_count=len(history))

    async def compacted_boundary(self, dialog_id: str) -> int:
        """Nothing is ever compacted: the narrative starts at the beginning."""
        return NO_COMPACTED_SEQ

    async def compact_now(self, dialog: Dialog) -> bool:
        """Never compacts: there is nothing to rebuild against."""
        return False

    async def aclose(self, dialog_id: str) -> None:
        """Nothing to cancel."""


class LlmContextCompactor(ContextCompactor):
    """Topics block + hot tail, compacting the overflow in a background task."""

    def __init__(
        self,
        store: SummaryStore,
        archive: MessageArchive,
        llm: LLMClient,
        config: CompactorConfig,
    ) -> None:
        self._store = store
        self._archive = archive
        self._llm = llm
        self._config = config
        self._running: dict[str, asyncio.Task[None]] = {}

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        """Return `[topics block?] + hot tail`; trigger compaction on overflow."""
        max_seq_to = await self._store.max_seq_to(dialog.id)
        tail_count = await self._archive.count_after(dialog.id, max_seq_to)
        tail = _tail_of(history, tail_count)
        if _chars(tail) > self._config.hot_max_chars or await self._token_overflow(
            dialog, max_seq_to
        ):
            self._trigger_compact(dialog)
        summaries = await self._store.list_for_dialog(dialog.id)
        if not summaries:
            return AssembledContext(messages=tail, tail_count=len(tail))
        return AssembledContext(messages=[_topics_block(summaries), *tail], tail_count=len(tail))

    async def compacted_boundary(self, dialog_id: str) -> int:
        """The narrative's hot slice starts right after the last compacted seq."""
        return await self._store.max_seq_to(dialog_id)

    async def _token_overflow(self, dialog: Dialog, after_seq: int) -> bool:
        """Whether the last run's prompt approached the model's context window.

        Only usage recorded past the compaction boundary counts: an assistant
        message compacted into the summary describes a pre-compaction prompt
        and must not retrigger compaction.
        """
        if self._config.model_context_tokens <= 0:
            return False
        prompt_tokens = await self._archive.latest_prompt_tokens(dialog.id, after_seq)
        if prompt_tokens is None:
            return False
        threshold = self._config.model_context_tokens - self._config.context_buffer_tokens
        return prompt_tokens >= threshold

    async def aclose(self, dialog_id: str) -> None:
        """Cancel the dialog's pending compaction (its runner is stopping).

        Scoped to the given dialog: the instance is shared by every runner of
        the manager, so other dialogs' compactions keep running.
        """
        task = self._running.pop(dialog_id, None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def compact_now(self, dialog: Dialog) -> bool:
        """Compact synchronously; True when the covered range advanced.

        Goes through the same one-run-per-dialog guard as the background
        trigger: an in-flight compaction (background or a concurrent
        compact_now) is awaited, and when it already advanced the boundary
        the call is done — the reactive path never redoes the same work.
        """
        before = await self._store.max_seq_to(dialog.id)
        running = self._running.get(dialog.id)
        if running is not None and not running.done():
            with suppress(asyncio.CancelledError):
                await running
            if await self._store.max_seq_to(dialog.id) > before:
                return True  # the awaited run already advanced the range
        # _start_compact re-checks the guard: an assemble may have triggered a
        # fresh run during the awaits above, and starting our own would race
        # it — two concurrent summarize+replace runs for one dialog
        task = self._start_compact(dialog)
        await task
        return await self._store.max_seq_to(dialog.id) > before

    def _trigger_compact(self, dialog: Dialog) -> None:
        """Start a background compaction unless one is already running (guard)."""
        self._start_compact(dialog)

    def _start_compact(self, dialog: Dialog) -> asyncio.Task[None]:
        """Return the dialog's running compaction, starting one when none is live.

        The single place enforcing one-compaction-per-dialog: every path
        (background trigger, reactive compact_now) goes through it, so two
        callers can never register two concurrent runs for one dialog.
        """
        running = self._running.get(dialog.id)
        if running is not None and not running.done():
            return running
        task = asyncio.create_task(self._compact(dialog))
        self._running[dialog.id] = task
        task.add_done_callback(lambda done: self._on_compact_done(dialog.id, done))
        return task

    def _on_compact_done(self, dialog_id: str, task: asyncio.Task[None]) -> None:
        if self._running.get(dialog_id) is task:  # a newer run may already be registered
            self._running.pop(dialog_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # a failed compaction degrades softly: log and move on
            logger.warning("context compaction failed: dialog=%s", dialog_id, exc_info=exc)

    async def _compact(self, dialog: Dialog) -> None:
        max_seq_to = await self._store.max_seq_to(dialog.id)
        tail = await self._archive.tail_after(dialog.id, max_seq_to, limit=SEGMENT_FETCH_LIMIT)
        segment = select_compact_segment(tail, self._config.compact_target_chars)
        if not segment:
            return
        previous = await self._store.list_for_dialog(dialog.id)
        summary = await self._summarize(dialog, segment, previous)
        await self._store.replace_for_dialog(dialog.id, summary)

    async def _summarize(
        self,
        dialog: Dialog,
        segment: list[ArchivedMessage],
        previous: list[DialogueSummary],
    ) -> DialogueSummary:
        completion = await self._llm.complete(
            [
                ChatMessage(role=MessageRole.SYSTEM, content=SUMMARY_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=format_merge_request(previous, segment)),
            ]
        )
        topics, content = parse_summary_reply(completion.message.content)
        seq_from = min([segment[0].seq, *(s.seq_from for s in previous)])
        return DialogueSummary(
            id=uuid.uuid4().hex,
            dialog_id=dialog.id,
            seq_from=seq_from,
            seq_to=segment[-1].seq,
            topics=topics,
            content=content,
            created_at=utc_now(),
        )


def select_compact_segment(
    tail: list[ArchivedMessage],
    target_chars: int,
) -> list[ArchivedMessage]:
    """Pick the oldest prefix of the tail up to `target_chars` of content.

    The cut happens only between whole messages, and the LAST message of the
    tail is never taken (keep-recent: the freshest message — typically the
    user's question being answered — always stays in the verbatim hot tail,
    so a compaction racing a new submit cannot swallow it). A salvaged pair —
    an assistant message followed by the INTERRUPTED_NOTE system note — is
    never split: a cut landing between the two is moved past the note, so
    both go into the summary; when the note is the kept-last message, the cut
    moves before the pair instead, so both stay in the tail.
    """
    segment: list[ArchivedMessage] = []
    total = 0
    for message in tail[:-1]:  # keep-recent: the last message never leaves the tail
        if segment and total + len(message.content) > target_chars:
            break
        segment.append(message)
        total += len(message.content)
    if segment and len(tail) > len(segment) and _is_split_pair(segment[-1], tail[len(segment)]):
        if len(segment) + 1 == len(tail):
            segment.pop()  # extending would swallow the kept-last note: cut before the pair
        else:
            segment.append(tail[len(segment)])
    return segment


def _is_split_pair(last_in: ArchivedMessage, first_out: ArchivedMessage) -> bool:
    return (
        last_in.role is MessageRole.ASSISTANT
        and first_out.role is MessageRole.SYSTEM
        and first_out.content == INTERRUPTED_NOTE
    )


def _tail_of(history: list[ChatMessage], tail_count: int) -> list[ChatMessage]:
    """Take the hot tail from the narrative; tolerate a shorter mirror (safe min)."""
    count = min(tail_count, len(history))
    if count == 0:
        return []
    return history[-count:]


def _chars(messages: list[ChatMessage]) -> int:
    return sum(len(message.content) for message in messages)


def _topics_block(summaries: list[DialogueSummary]) -> ChatMessage:
    lines = [TOPICS_BLOCK_HEADER]
    for summary in summaries:
        lines.append(
            TOPIC_ENTRY_TEMPLATE.format(
                seq_from=summary.seq_from,
                seq_to=summary.seq_to,
                topics=", ".join(summary.topics) if summary.topics else NO_TOPICS,
                content=summary.content,
            )
        )
    return ChatMessage(role=MessageRole.SYSTEM, content="\n".join(lines))
