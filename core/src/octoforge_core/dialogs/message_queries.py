"""Message lookup, narrative reads and exchange attachment."""

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context.models import SummaryRow
from octoforge_core.context.types import NO_COMPACTED_SEQ
from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.dialogs._rows import to_chat_message
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.domain import ChatMessage

ChatMessageList = list[ChatMessage]


class MessageQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def find_by_client_id(self, dialog_id: str, client_message_id: str) -> bool:
        async with read_session(self._sessions) as session:
            value = await session.scalar(
                select(MessageRow.id)
                .where(
                    MessageRow.dialog_id == dialog_id,
                    MessageRow.client_message_id == client_message_id,
                )
                .limit(1)
            )
            return value is not None

    async def list_hot_slice(self, dialog_id: str) -> list[ChatMessage]:
        boundary = (
            select(func.coalesce(func.max(SummaryRow.seq_to), NO_COMPACTED_SEQ))
            .where(SummaryRow.dialog_id == dialog_id)
            .scalar_subquery()
        )
        return await self._messages_after(dialog_id, boundary)

    async def list_after(self, dialog_id: str, after_seq: int) -> list[ChatMessage]:
        return await self._messages_after(dialog_id, after_seq)

    async def list(self, dialog_id: str) -> ChatMessageList:
        async with read_session(self._sessions) as session:
            rows = (
                await session.scalars(
                    select(MessageRow)
                    .where(MessageRow.dialog_id == dialog_id)
                    .order_by(MessageRow.seq)
                )
            ).all()
            return [to_chat_message(row) for row in rows]

    async def set_exchange(self, message_id: str, exchange_id: str) -> None:
        async with write_session(self._sessions) as session:
            await session.execute(
                update(MessageRow)
                .where(MessageRow.id == message_id)
                .values(exchange_id=exchange_id)
            )

    async def _messages_after(
        self,
        dialog_id: str,
        after_seq: int | ColumnElement[int],
    ) -> ChatMessageList:
        async with read_session(self._sessions) as session:
            rows = (
                await session.scalars(
                    select(MessageRow)
                    .where(MessageRow.dialog_id == dialog_id, MessageRow.seq > after_seq)
                    .order_by(MessageRow.seq)
                )
            ).all()
            return [to_chat_message(row) for row in rows]
