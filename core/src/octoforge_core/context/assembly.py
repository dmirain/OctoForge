"""Assemble topics plus hot tail and decide when compaction is needed."""

import asyncio
from collections.abc import Callable

from octoforge_core.context.compaction_policy import (
    CompactorConfig,
    content_chars,
    tail_of,
    topics_block,
)
from octoforge_core.context.ports import MessageArchive, SummaryStore
from octoforge_core.context.types import NO_COMPACTED_SEQ, AssembledContext
from octoforge_core.domain import ChatMessage, Dialog


class ContextAssembler:
    """Build one narrative branch from rolling topics and an in-memory hot tail."""

    def __init__(
        self,
        store: SummaryStore,
        archive: MessageArchive,
        config: CompactorConfig,
    ) -> None:
        self._store = store
        self._archive = archive
        self._config = config

    async def assemble(
        self,
        dialog: Dialog,
        history: list[ChatMessage],
        trigger: Callable[[Dialog], None],
    ) -> AssembledContext:
        summaries, (tail_count, counted_boundary) = await asyncio.gather(
            self._store.list_for_dialog(dialog.id),
            self._archive.count_hot_tail(dialog.id),
        )
        boundary = max((summary.seq_to for summary in summaries), default=NO_COMPACTED_SEQ)
        if counted_boundary != boundary:
            summaries = await self._store.list_for_dialog(dialog.id)
            boundary = max((summary.seq_to for summary in summaries), default=NO_COMPACTED_SEQ)
        snapshot_len = len(history)
        tail = tail_of(history, tail_count)
        if content_chars(tail) > self._config.hot_max_chars or await self._token_overflow(
            dialog,
            boundary,
        ):
            trigger(dialog)
        messages = tail if not summaries else [topics_block(summaries), *tail]
        return AssembledContext(messages, len(tail), snapshot_len)

    async def _token_overflow(self, dialog: Dialog, after_seq: int) -> bool:
        if self._config.model_context_tokens <= 0:
            return False
        prompt_tokens = await self._archive.latest_prompt_tokens(dialog.id, after_seq)
        if prompt_tokens is None:
            return False
        threshold = self._config.model_context_tokens - self._config.context_buffer_tokens
        return prompt_tokens >= threshold
