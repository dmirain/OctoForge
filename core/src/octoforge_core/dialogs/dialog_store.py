"""SQL dialog registry keyed by user and channel."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.dialogs._rows import to_dialog
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.dialogs.types import DialogNotFoundError
from octoforge_core.domain import Dialog


class SqlAlchemyDialogRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get_or_create(self, user_id: str, channel: str) -> Dialog:
        async with read_session(self._sessions) as session:
            row = await _find_row(session, user_id, channel)
            if row is not None:
                return to_dialog(row)
        async with write_session(self._sessions) as session:
            created = DialogRow(id=uuid.uuid4().hex, user_id=user_id, channel=channel)
            session.add(created)
            await session.flush()
            return to_dialog(created)

    async def get(self, dialog_id: str) -> Dialog:
        async with read_session(self._sessions) as session:
            row = await session.get(DialogRow, dialog_id)
            if row is None:
                raise DialogNotFoundError(dialog_id)
            return to_dialog(row)

    async def list_by_channel(self, channel: str) -> list[Dialog]:
        async with read_session(self._sessions) as session:
            rows = (
                await session.scalars(select(DialogRow).where(DialogRow.channel == channel))
            ).all()
            return [to_dialog(row) for row in rows]

    async def delete(self, dialog_id: str) -> None:
        async with write_session(self._sessions) as session:
            row = await session.get(DialogRow, dialog_id)
            if row is None:
                raise DialogNotFoundError(dialog_id)
            await session.execute(delete(MessageRow).where(MessageRow.dialog_id == dialog_id))
            await session.delete(row)


async def _find_row(session: AsyncSession, user_id: str, channel: str) -> DialogRow | None:
    rows = await session.scalars(
        select(DialogRow).where(DialogRow.user_id == user_id, DialogRow.channel == channel)
    )
    return rows.first()
