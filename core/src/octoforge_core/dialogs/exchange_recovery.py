"""Queries and reset for exchanges left without a live task."""

from typing import Any, cast

from sqlalchemy import Exists, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.dialogs._rows import to_exchange
from octoforge_core.dialogs.models import ExchangeRow
from octoforge_core.dialogs.types import ExchangeList, ExchangeStatus
from octoforge_core.tasks.api import TaskStatus
from octoforge_core.tasks.models import TaskRow


async def list_unowned_open(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str | None,
) -> ExchangeList:
    query = (
        select(ExchangeRow)
        .where(ExchangeRow.status == ExchangeStatus.OPEN.value, ~has_live_task())
        .order_by(ExchangeRow.created_at)
    )
    if dialog_id is not None:
        query = query.where(ExchangeRow.dialog_id == dialog_id)
    async with read_session(session_factory) as session:
        rows = (await session.scalars(query)).all()
        return [to_exchange(row) for row in rows]


async def list_stranded_dialog_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    async with read_session(session_factory) as session:
        rows = await session.scalars(
            select(ExchangeRow.dialog_id)
            .where(
                (ExchangeRow.status == ExchangeStatus.IN_PROGRESS.value)
                | ((ExchangeRow.status == ExchangeStatus.OPEN.value) & ~has_live_task())
            )
            .distinct()
        )
        return list(rows.all())


async def reopen_and_list_stranded(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
) -> tuple[int, ExchangeList]:
    async with write_session(session_factory) as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(ExchangeRow)
                .where(
                    ExchangeRow.dialog_id == dialog_id,
                    ExchangeRow.status == ExchangeStatus.IN_PROGRESS.value,
                )
                .values(status=ExchangeStatus.OPEN.value)
            ),
        )
        stranded = await session.scalars(
            select(ExchangeRow)
            .where(
                ExchangeRow.dialog_id == dialog_id,
                ExchangeRow.status == ExchangeStatus.OPEN.value,
                ~has_live_task(),
            )
            .order_by(ExchangeRow.created_at)
        )
        return result.rowcount or 0, [to_exchange(row) for row in stranded.all()]


def has_live_task() -> Exists:
    return (
        select(TaskRow.id)
        .where(
            TaskRow.exchange_id == ExchangeRow.id,
            TaskRow.status.in_((TaskStatus.PENDING.value, TaskStatus.RUNNING.value)),
        )
        .exists()
    )
