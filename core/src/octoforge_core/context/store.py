"""SQLAlchemy storage of the context module (module-internal).

Follows the `memory/store.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to DTOs at the boundary. One class
implements both ports of the module: `SummaryStore` over the module-owned
`dialog_summaries` table and `MessageArchive` as a read-only view over the
`messages` table (writing messages stays the actor's business).
"""

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context.api import (
    NO_COMPACTED_SEQ,
    ArchivedMessage,
    ArchiveFilter,
    DialogueSummary,
    MessageArchive,
    SummaryStore,
)
from octoforge_core.context.models import SummaryRow
from octoforge_core.db.models import MessageRow
from octoforge_core.domain import MessageRole

LIKE_ESCAPE = "\\"


class SqlAlchemySummaryStore(SummaryStore, MessageArchive):
    """SQL persistence for dialog summaries plus read access to the archive."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, summary: DialogueSummary) -> DialogueSummary:
        """Persist a fully populated summary; return the stored copy."""
        async with self._session_factory() as session:
            session.add(
                SummaryRow(
                    id=summary.id,
                    dialog_id=summary.dialog_id,
                    seq_from=summary.seq_from,
                    seq_to=summary.seq_to,
                    topics=list(summary.topics),
                    content=summary.content,
                    created_at=summary.created_at,
                )
            )
            await session.commit()
        return summary

    async def list_for_dialog(self, dialog_id: str) -> list[DialogueSummary]:
        """Return all summaries of the dialog, ordered by seq_from."""
        async with self._session_factory() as session:
            result = await session.scalars(
                select(SummaryRow)
                .where(SummaryRow.dialog_id == dialog_id)
                .order_by(SummaryRow.seq_from)
            )
            return [_to_summary(row) for row in result.all()]

    async def replace_for_dialog(self, dialog_id: str, summary: DialogueSummary) -> None:
        """Replace all summaries of the dialog with the one (rolling merge)."""
        async with self._session_factory() as session:
            await session.execute(delete(SummaryRow).where(SummaryRow.dialog_id == dialog_id))
            session.add(
                SummaryRow(
                    id=summary.id,
                    dialog_id=summary.dialog_id,
                    seq_from=summary.seq_from,
                    seq_to=summary.seq_to,
                    topics=list(summary.topics),
                    content=summary.content,
                    created_at=summary.created_at,
                )
            )
            await session.commit()

    async def max_seq_to(self, dialog_id: str) -> int:
        """Return the highest covered seq; NO_COMPACTED_SEQ when nothing was compacted."""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(func.coalesce(func.max(SummaryRow.seq_to), NO_COMPACTED_SEQ)).where(
                    SummaryRow.dialog_id == dialog_id
                )
            )
            return int(value or NO_COMPACTED_SEQ)

    async def find_by_topic(self, dialog_id: str, topic: str) -> list[DialogueSummary]:
        """Return the dialog's summaries tagged with the topic (case-insensitive).

        The summaries of one dialog are few by design (they all go into the
        prompt), so tag matching happens in memory rather than in JSON SQL.
        """
        needle = topic.strip().lower()
        if not needle:
            return []
        summaries = await self.list_for_dialog(dialog_id)
        return [s for s in summaries if needle in {tag.lower() for tag in s.topics}]

    async def count_after(self, dialog_id: str, seq: int) -> int:
        """Return how many messages the dialog has with `seq >` the given one."""
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(MessageRow.id)).where(
                    MessageRow.dialog_id == dialog_id,
                    MessageRow.seq > seq,
                )
            )
            return int(count or 0)

    async def tail_after(
        self, dialog_id: str, seq: int, limit: int | None = None
    ) -> list[ArchivedMessage]:
        """Return the dialog messages with `seq >` the given one, ordered by seq."""
        async with self._session_factory() as session:
            statement = (
                select(MessageRow)
                .where(MessageRow.dialog_id == dialog_id, MessageRow.seq > seq)
                .order_by(MessageRow.seq)
            )
            if limit is not None:
                statement = statement.limit(limit)
            result = await session.scalars(statement)
            return [_to_archived(row) for row in result.all()]

    async def latest_prompt_tokens(self, dialog_id: str, after_seq: int) -> int | None:
        """Return prompt_tokens of the newest tail assistant message with usage."""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(MessageRow.prompt_tokens)
                .where(
                    MessageRow.dialog_id == dialog_id,
                    MessageRow.seq > after_seq,
                    MessageRow.role == MessageRole.ASSISTANT.value,
                    MessageRow.prompt_tokens.is_not(None),
                )
                .order_by(MessageRow.seq.desc())
                .limit(1)
            )
            return int(value) if value is not None else None

    async def search(
        self,
        dialog_id: str,
        query: str,
        *,
        filters: ArchiveFilter | None = None,
        limit: int,
    ) -> list[ArchivedMessage]:
        """Return archive messages matching the substring; see the port contract."""
        needle = query.strip()
        restriction = filters if filters is not None else ArchiveFilter()
        if not needle or restriction.seq_ranges == ():
            return []
        clauses = [
            MessageRow.dialog_id == dialog_id,
            MessageRow.content.ilike(f"%{_escape_like(needle)}%", escape=LIKE_ESCAPE),
        ]
        if restriction.seq_ranges is not None:
            clauses.append(
                or_(
                    *(
                        and_(MessageRow.seq >= seq_from, MessageRow.seq <= seq_to)
                        for seq_from, seq_to in restriction.seq_ranges
                    )
                )
            )
        if restriction.date_from is not None:
            clauses.append(MessageRow.created_at >= restriction.date_from)
        if restriction.date_to is not None:
            clauses.append(MessageRow.created_at < restriction.date_to)
        async with self._session_factory() as session:
            result = await session.scalars(
                select(MessageRow).where(*clauses).order_by(MessageRow.seq).limit(limit)
            )
            return [_to_archived(row) for row in result.all()]


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so the query stays a literal substring."""
    return (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def _to_summary(row: SummaryRow) -> DialogueSummary:
    return DialogueSummary(
        id=row.id,
        dialog_id=row.dialog_id,
        seq_from=row.seq_from,
        seq_to=row.seq_to,
        topics=tuple(row.topics),
        content=row.content,
        created_at=row.created_at,
    )


def _to_archived(row: MessageRow) -> ArchivedMessage:
    return ArchivedMessage(
        seq=row.seq,
        role=MessageRole(row.role),
        content=row.content,
        created_at=row.created_at,
    )
