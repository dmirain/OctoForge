"""Portable SQL reads over the complete dialog message archive."""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context._rows import to_archived
from octoforge_core.context.models import SummaryRow
from octoforge_core.context.requests import ArchiveSearch, ArchiveTail
from octoforge_core.context.types import NO_COMPACTED_SEQ, ArchivedMessage, ArchiveFilter
from octoforge_core.db.unit_of_work import read_session
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.domain import MessageRole

LIKE_ESCAPE = "\\"


async def count_hot_tail(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
) -> tuple[int, int]:
    boundary = (
        select(func.coalesce(func.max(SummaryRow.seq_to), NO_COMPACTED_SEQ))
        .where(SummaryRow.dialog_id == dialog_id)
        .scalar_subquery()
    )
    async with read_session(session_factory) as session:
        count, counted_boundary = (
            await session.execute(
                select(
                    func.count(MessageRow.id).filter(MessageRow.seq > boundary),
                    boundary,
                ).where(MessageRow.dialog_id == dialog_id)
            )
        ).one()
        return int(count or 0), int(counted_boundary)


async def tail_after(
    session_factory: async_sessionmaker[AsyncSession],
    request: ArchiveTail,
) -> list[ArchivedMessage]:
    statement = (
        select(MessageRow)
        .where(MessageRow.dialog_id == request.dialog_id, MessageRow.seq > request.after_seq)
        .order_by(MessageRow.seq)
    )
    if request.limit is not None:
        statement = statement.limit(request.limit)
    async with read_session(session_factory) as session:
        rows = (await session.scalars(statement)).all()
        return [to_archived(row) for row in rows]


async def latest_prompt_tokens(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
    after_seq: int,
) -> int | None:
    async with read_session(session_factory) as session:
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


async def search_archive(
    session_factory: async_sessionmaker[AsyncSession],
    request: ArchiveSearch,
) -> list[ArchivedMessage]:
    needle = request.query.strip()
    restriction = request.filters if request.filters is not None else ArchiveFilter()
    if not needle or restriction.seq_ranges == ():
        return []
    clauses = [
        MessageRow.dialog_id == request.dialog_id,
        MessageRow.content.ilike(f"%{_escape_like(needle)}%", escape=LIKE_ESCAPE),
    ]
    if restriction.seq_ranges is not None:
        clauses.append(
            or_(
                *(
                    and_(MessageRow.seq >= start, MessageRow.seq <= end)
                    for start, end in restriction.seq_ranges
                )
            )
        )
    if restriction.date_from is not None:
        clauses.append(MessageRow.created_at >= restriction.date_from)
    if restriction.date_to is not None:
        clauses.append(MessageRow.created_at < restriction.date_to)
    async with read_session(session_factory) as session:
        rows = (
            await session.scalars(
                select(MessageRow).where(*clauses).order_by(MessageRow.seq).limit(request.limit)
            )
        ).all()
        return [to_archived(row) for row in rows]


def _escape_like(text: str) -> str:
    return (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
