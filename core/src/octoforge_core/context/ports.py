"""Compaction, summary and archive ports."""

from typing import Protocol

from octoforge_core.context.requests import ArchiveSearch
from octoforge_core.context.types import ArchivedMessage, AssembledContext, DialogueSummary
from octoforge_core.domain import ChatMessage, Dialog


class ContextCompactor(Protocol):
    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext: ...

    async def compacted_boundary(self, dialog_id: str) -> int: ...

    async def compact_now(self, dialog: Dialog) -> bool: ...

    async def aclose(self, dialog_id: str) -> None: ...


class SummaryStore(Protocol):
    async def list_for_dialog(self, dialog_id: str) -> list[DialogueSummary]: ...

    async def replace_for_dialog(self, dialog_id: str, summary: DialogueSummary) -> None: ...

    async def max_seq_to(self, dialog_id: str) -> int: ...

    async def find_by_topic(self, dialog_id: str, topic: str) -> list[DialogueSummary]: ...

    async def delete_for_dialog(self, dialog_id: str) -> None: ...


class MessageArchive(Protocol):
    async def count_hot_tail(self, dialog_id: str) -> tuple[int, int]: ...

    async def tail_after(
        self,
        dialog_id: str,
        seq: int,
        limit: int | None = None,
    ) -> list[ArchivedMessage]: ...

    async def latest_prompt_tokens(self, dialog_id: str, after_seq: int) -> int | None: ...

    async def search(self, request: ArchiveSearch) -> list[ArchivedMessage]: ...
