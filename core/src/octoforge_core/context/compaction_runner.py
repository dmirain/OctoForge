"""One archive-segment compaction from read through summary replacement."""

from dataclasses import dataclass

from octoforge_core.context.compaction_policy import (
    SEGMENT_FETCH_LIMIT,
    CompactorConfig,
    select_compact_segment,
)
from octoforge_core.context.ports import MessageArchive, SummaryStore
from octoforge_core.context.summary_generator import SummaryGenerator
from octoforge_core.domain import Dialog


@dataclass(frozen=True, slots=True)
class CompactionServices:
    store: SummaryStore
    archive: MessageArchive
    generator: SummaryGenerator


class CompactionRunner:
    """Advance one dialog's compacted boundary by at most one segment."""

    def __init__(self, services: CompactionServices, config: CompactorConfig) -> None:
        self._store = services.store
        self._archive = services.archive
        self._generator = services.generator
        self._config = config

    async def run(self, dialog: Dialog) -> None:
        boundary = await self._store.max_seq_to(dialog.id)
        tail = await self._archive.tail_after(dialog.id, boundary, limit=SEGMENT_FETCH_LIMIT)
        segment = select_compact_segment(tail, self._config.compact_target_chars)
        if not segment:
            return
        previous = await self._store.list_for_dialog(dialog.id)
        summary = await self._generator.generate(dialog, segment, previous)
        await self._store.replace_for_dialog(dialog.id, summary)
