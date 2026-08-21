"""SQL adapter implementing summary persistence and archive reads."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context._archive_store import (
    count_hot_tail,
    latest_prompt_tokens,
    search_archive,
    tail_after,
)
from octoforge_core.context._rows import to_archived
from octoforge_core.context._summary_store import (
    create_summary,
    delete_summaries,
    list_summaries,
    max_seq_to,
    replace_summaries,
)
from octoforge_core.context.api import (
    ArchivedMessage,
    ArchiveSearch,
    DialogueSummary,
    MessageArchive,
    SummaryStore,
)
from octoforge_core.context.requests import ArchiveTail

__all__ = ["SqlAlchemySummaryStore", "to_archived"]


class SqlAlchemySummaryStore(SummaryStore, MessageArchive):
    """One adapter over rolling summaries and the full message archive."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, summary: DialogueSummary) -> DialogueSummary:
        return await create_summary(self._session_factory, summary)

    async def list_for_dialog(self, dialog_id: str) -> list[DialogueSummary]:
        return await list_summaries(self._session_factory, dialog_id)

    async def replace_for_dialog(self, dialog_id: str, summary: DialogueSummary) -> None:
        await replace_summaries(self._session_factory, dialog_id, summary)

    async def delete_for_dialog(self, dialog_id: str) -> None:
        await delete_summaries(self._session_factory, dialog_id)

    async def max_seq_to(self, dialog_id: str) -> int:
        return await max_seq_to(self._session_factory, dialog_id)

    async def find_by_topic(self, dialog_id: str, topic: str) -> list[DialogueSummary]:
        needle = topic.strip().lower()
        if not needle:
            return []
        summaries = await self.list_for_dialog(dialog_id)
        return [item for item in summaries if needle in {tag.lower() for tag in item.topics}]

    async def count_hot_tail(self, dialog_id: str) -> tuple[int, int]:
        return await count_hot_tail(self._session_factory, dialog_id)

    async def tail_after(
        self,
        dialog_id: str,
        seq: int,
        limit: int | None = None,
    ) -> list[ArchivedMessage]:
        return await tail_after(self._session_factory, ArchiveTail(dialog_id, seq, limit))

    async def latest_prompt_tokens(self, dialog_id: str, after_seq: int) -> int | None:
        return await latest_prompt_tokens(self._session_factory, dialog_id, after_seq)

    async def search(self, request: ArchiveSearch) -> list[ArchivedMessage]:
        return await search_archive(self._session_factory, request)
