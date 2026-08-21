"""Public context compactor implementations assembled from internal decisions."""

from dataclasses import dataclass

from octoforge_core.context.assembly import ContextAssembler
from octoforge_core.context.compaction_jobs import DialogCompactions
from octoforge_core.context.compaction_policy import (
    SEGMENT_FETCH_LIMIT,
    CompactorConfig,
    select_compact_segment,
)
from octoforge_core.context.compaction_runner import CompactionRunner, CompactionServices
from octoforge_core.context.ports import ContextCompactor, MessageArchive, SummaryStore
from octoforge_core.context.summary_generator import SummaryGenerator
from octoforge_core.context.types import NO_COMPACTED_SEQ, AssembledContext
from octoforge_core.domain import ChatMessage, Dialog
from octoforge_core.ports import LLMClient
from octoforge_core.tariffs.api import UsageRecorder

__all__ = [
    "SEGMENT_FETCH_LIMIT",
    "CompactorConfig",
    "CompactorServices",
    "LlmContextCompactor",
    "NoopContextCompactor",
    "select_compact_segment",
]


@dataclass(frozen=True, slots=True)
class CompactorServices:
    store: SummaryStore
    archive: MessageArchive
    llm: LLMClient
    meter: UsageRecorder | None = None


class NoopContextCompactor(ContextCompactor):
    """Pass through the full narrative without compaction."""

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        return AssembledContext(list(history), len(history), len(history))

    async def compacted_boundary(self, dialog_id: str) -> int:
        return NO_COMPACTED_SEQ

    async def compact_now(self, dialog: Dialog) -> bool:
        return False

    async def aclose(self, dialog_id: str) -> None:
        return None


class LlmContextCompactor(ContextCompactor):
    """Topics plus hot tail with one background compaction per dialog."""

    def __init__(self, services: CompactorServices, config: CompactorConfig) -> None:
        generator = SummaryGenerator(services.llm, services.meter)
        runner = CompactionRunner(
            CompactionServices(services.store, services.archive, generator),
            config,
        )
        self._store = services.store
        self._assembler = ContextAssembler(services.store, services.archive, config)
        self._jobs = DialogCompactions(services.store, runner)

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        return await self._assembler.assemble(dialog, history, self._jobs.trigger)

    async def compacted_boundary(self, dialog_id: str) -> int:
        return await self._store.max_seq_to(dialog_id)

    async def compact_now(self, dialog: Dialog) -> bool:
        return await self._jobs.compact_now(dialog)

    async def aclose(self, dialog_id: str) -> None:
        await self._jobs.close(dialog_id)
