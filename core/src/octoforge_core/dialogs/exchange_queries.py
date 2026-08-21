"""Reads and recovery projections for durable exchanges."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session
from octoforge_core.dialogs._rows import to_exchange
from octoforge_core.dialogs.exchange_recovery import (
    list_stranded_dialog_ids,
    list_unowned_open,
    reopen_and_list_stranded,
)
from octoforge_core.dialogs.models import ExchangeRow
from octoforge_core.dialogs.types import (
    LIVE_EXCHANGE_STATUSES,
    Exchange,
    ExchangeList,
    ExchangeNotFoundError,
    ExchangeStatus,
)
from octoforge_core.time import utc_now


class ExchangeQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(self, exchange_id: str) -> Exchange:
        async with read_session(self._sessions) as session:
            row = await session.get(ExchangeRow, exchange_id)
            if row is None:
                raise ExchangeNotFoundError(exchange_id)
            return to_exchange(row)

    async def find_collecting(self, dialog_id: str) -> Exchange | None:
        async with read_session(self._sessions) as session:
            row = (
                await session.scalars(
                    select(ExchangeRow)
                    .where(
                        ExchangeRow.dialog_id == dialog_id,
                        ExchangeRow.status == ExchangeStatus.COLLECTING.value,
                    )
                    .order_by(ExchangeRow.created_at)
                    .limit(1)
                )
            ).first()
            return to_exchange(row) if row is not None else None

    async def list_stale_collecting(self, quiet_seconds: float) -> ExchangeList:
        threshold = utc_now() - timedelta(seconds=quiet_seconds)
        async with read_session(self._sessions) as session:
            rows = (
                await session.scalars(
                    select(ExchangeRow)
                    .where(
                        ExchangeRow.status == ExchangeStatus.COLLECTING.value,
                        ExchangeRow.updated_at < threshold,
                    )
                    .order_by(ExchangeRow.updated_at)
                )
            ).all()
            return [to_exchange(row) for row in rows]

    async def list_live(self, dialog_id: str) -> ExchangeList:
        async with read_session(self._sessions) as session:
            rows = (
                await session.scalars(
                    select(ExchangeRow)
                    .where(
                        ExchangeRow.dialog_id == dialog_id,
                        ExchangeRow.status.in_([status.value for status in LIVE_EXCHANGE_STATUSES]),
                    )
                    .order_by(ExchangeRow.created_at)
                )
            ).all()
            return [to_exchange(row) for row in rows]

    async def list_unowned_open(self, dialog_id: str | None = None) -> ExchangeList:
        return await list_unowned_open(self._sessions, dialog_id)

    async def list_stranded_dialog_ids(self) -> list[str]:
        return await list_stranded_dialog_ids(self._sessions)

    async def reopen_and_list_stranded(self, dialog_id: str) -> tuple[int, ExchangeList]:
        return await reopen_and_list_stranded(self._sessions, dialog_id)
