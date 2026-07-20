"""Default ContextCompactor: hot-tail policy and background LLM summarization.

Assemble returns `[topics block?] + hot tail`: one system message with every
summary of the dialog (skipped when there are none), then the archive tail
with `seq > max(seq_to)` taken from the actor's narrative (its in-memory
mirror). When the tail outgrows the configured limit, a background asyncio
task compresses the oldest tail messages into a new summary — one LLM call,
one store write; the fresh tail is never touched. Compaction is technical
work, not an actor process: it stays invisible to the dialog, guarded to one
run per dialog, and a failure is a logged warning, never a dialog error.
"""

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.agent.runner import INTERRUPTED_NOTE
from octoforge_core.context.api import (
    ArchivedMessage,
    ContextCompactor,
    DialogueSummary,
    MessageArchive,
    SummaryStore,
)
from octoforge_core.context.prompts import (
    SUMMARY_SYSTEM_PROMPT,
    format_segment,
    parse_summary_reply,
)
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.ports import LLMClient
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

DEFAULT_HOT_MAX_CHARS = 12000
DEFAULT_COMPACT_TARGET_CHARS = 6000
TOPICS_BLOCK_HEADER = (
    "Compressed summaries of earlier topics of this conversation "
    "(the verbatim recent history follows):"
)
TOPIC_ENTRY_TEMPLATE = "[seq {seq_from}-{seq_to}] (topics: {topics}) {content}"
NO_TOPICS = "-"


@dataclass(frozen=True, slots=True)
class CompactorConfig:
    """Limits of the hot-tail policy, in characters (a ~4:1 proxy of tokens)."""

    hot_max_chars: int = DEFAULT_HOT_MAX_CHARS
    compact_target_chars: int = DEFAULT_COMPACT_TARGET_CHARS


class NoopContextCompactor(ContextCompactor):
    """ContextCompactor passthrough: the full narrative, never any compaction."""

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> list[ChatMessage]:
        """Return the history unchanged."""
        return list(history)

    async def aclose(self) -> None:
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

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> list[ChatMessage]:
        """Return `[topics block?] + hot tail`; trigger compaction on overflow."""
        max_seq_to = await self._store.max_seq_to(dialog.id)
        tail_count = await self._archive.count_after(dialog.id, max_seq_to)
        tail = _tail_of(history, tail_count)
        if _chars(tail) > self._config.hot_max_chars:
            self._trigger_compact(dialog)
        summaries = await self._store.list_for_dialog(dialog.id)
        if not summaries:
            return tail
        return [_topics_block(summaries), *tail]

    async def aclose(self) -> None:
        """Cancel pending compactions (the owning runner is stopping)."""
        tasks = list(self._running.values())
        self._running.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    def _trigger_compact(self, dialog: Dialog) -> None:
        """Start a background compaction unless one is already running (guard)."""
        running = self._running.get(dialog.id)
        if running is not None and not running.done():
            return  # one compaction per dialog at a time; the next assemble re-triggers
        task = asyncio.create_task(self._compact(dialog))
        self._running[dialog.id] = task
        task.add_done_callback(lambda done: self._on_compact_done(dialog.id, done))

    def _on_compact_done(self, dialog_id: str, task: asyncio.Task[None]) -> None:
        self._running.pop(dialog_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # a failed compaction degrades softly: log and move on
            logger.warning("context compaction failed: dialog=%s", dialog_id, exc_info=exc)

    async def _compact(self, dialog: Dialog) -> None:
        max_seq_to = await self._store.max_seq_to(dialog.id)
        tail = await self._archive.tail_after(dialog.id, max_seq_to)
        segment = select_compact_segment(tail, self._config.compact_target_chars)
        if not segment:
            return
        summary = await self._summarize(dialog, segment)
        await self._store.create(summary)

    async def _summarize(self, dialog: Dialog, segment: list[ArchivedMessage]) -> DialogueSummary:
        reply = await self._llm.complete(
            [
                ChatMessage(role=MessageRole.SYSTEM, content=SUMMARY_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=format_segment(segment)),
            ]
        )
        topics, content = parse_summary_reply(reply.content)
        return DialogueSummary(
            id=uuid.uuid4().hex,
            dialog_id=dialog.id,
            seq_from=segment[0].seq,
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

    The cut happens only between whole messages (at least one message is
    always taken). A salvaged pair — an assistant message followed by the
    INTERRUPTED_NOTE system note — is never split: a cut landing between the
    two is moved past the note, so both go into the summary.
    """
    segment: list[ArchivedMessage] = []
    total = 0
    for message in tail:
        if segment and total + len(message.content) > target_chars:
            break
        segment.append(message)
        total += len(message.content)
    if segment and len(tail) > len(segment) and _is_split_pair(segment[-1], tail[len(segment)]):
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
