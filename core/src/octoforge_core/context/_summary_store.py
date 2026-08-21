"""Persistence decisions for rolling dialog summaries."""

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context._rows import to_summary
from octoforge_core.context.models import SummaryRow
from octoforge_core.context.types import NO_COMPACTED_SEQ, DialogueSummary
from octoforge_core.db.unit_of_work import read_session, write_session


def summary_row(summary: DialogueSummary) -> SummaryRow:
    return SummaryRow(
        id=summary.id,
        dialog_id=summary.dialog_id,
        seq_from=summary.seq_from,
        seq_to=summary.seq_to,
        topics=list(summary.topics),
        content=summary.content,
        created_at=summary.created_at,
    )


async def create_summary(
    session_factory: async_sessionmaker[AsyncSession],
    summary: DialogueSummary,
) -> DialogueSummary:
    async with write_session(session_factory) as session:
        session.add(summary_row(summary))
    return summary


async def list_summaries(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
) -> list[DialogueSummary]:
    async with read_session(session_factory) as session:
        rows = (
            await session.scalars(
                select(SummaryRow)
                .where(SummaryRow.dialog_id == dialog_id)
                .order_by(SummaryRow.seq_from)
            )
        ).all()
        return [to_summary(row) for row in rows]


async def replace_summaries(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
    summary: DialogueSummary,
) -> None:
    async with write_session(session_factory) as session:
        await session.execute(delete(SummaryRow).where(SummaryRow.dialog_id == dialog_id))
        session.add(summary_row(summary))


async def delete_summaries(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
) -> None:
    async with write_session(session_factory) as session:
        await session.execute(delete(SummaryRow).where(SummaryRow.dialog_id == dialog_id))


async def max_seq_to(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
) -> int:
    async with read_session(session_factory) as session:
        value = await session.scalar(
            select(func.coalesce(func.max(SummaryRow.seq_to), NO_COMPACTED_SEQ)).where(
                SummaryRow.dialog_id == dialog_id
            )
        )
        return int(value or NO_COMPACTED_SEQ)
