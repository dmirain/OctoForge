"""Creation and direct state changes of durable exchanges."""

import uuid
from typing import Any, cast

from sqlalchemy import delete, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import write_session
from octoforge_core.dialogs._rows import to_exchange
from octoforge_core.dialogs.models import ExchangeRow
from octoforge_core.dialogs.types import (
    TITLE_MAX_LENGTH,
    Exchange,
    ExchangeNotFoundError,
    ExchangeStatus,
)
from octoforge_core.time import utc_now


class ExchangeCommands:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create(
        self,
        dialog_id: str,
        title: str,
        status: ExchangeStatus | None = None,
    ) -> Exchange:
        async with write_session(self._sessions) as session:
            row = ExchangeRow(
                id=uuid.uuid4().hex,
                dialog_id=dialog_id,
                status=(status or ExchangeStatus.OPEN).value,
                title=title[:TITLE_MAX_LENGTH],
            )
            session.add(row)
            await session.flush()
            return to_exchange(row)

    async def touch(self, exchange_id: str) -> None:
        async with write_session(self._sessions) as session:
            await session.execute(
                update(ExchangeRow)
                .where(ExchangeRow.id == exchange_id)
                .values(updated_at=utc_now())
            )

    async def set_title(self, exchange_id: str, title: str) -> None:
        async with write_session(self._sessions) as session:
            await session.execute(
                update(ExchangeRow)
                .where(ExchangeRow.id == exchange_id)
                .values(title=title[:TITLE_MAX_LENGTH])
            )

    async def set_status(
        self,
        exchange_id: str,
        status: ExchangeStatus,
        pending_question: str | None = None,
    ) -> None:
        async with write_session(self._sessions) as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(ExchangeRow)
                    .where(ExchangeRow.id == exchange_id)
                    .values(status=status.value, pending_question=pending_question)
                ),
            )
            if result.rowcount == 0:
                raise ExchangeNotFoundError(exchange_id)

    async def delete_for_dialog(self, dialog_id: str) -> None:
        async with write_session(self._sessions) as session:
            await session.execute(delete(ExchangeRow).where(ExchangeRow.dialog_id == dialog_id))
