"""Where a live answer is being written, remembered across processes.

A draft is "which Telegram message am I editing for this exchange". It lives
in the bridge's memory, which is fine while one process owns a dialog for its
whole life — and wrong the moment dialogs move between processes: the new
owner would start a *second* message, and the user would see a truncated
answer followed by a complete one.

So the message id is written down. Only the parts that cannot be recreated
are stored — which message, what it replies to, how many chunks of a long
answer went before it. The text is not: the new owner re-answers and edits
the same message until it holds the finished reply.

Written once per message created, not per edit: an edit that only appends
text changes nothing anyone else would need.
"""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Integer, String, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_web.telegram.schema import TelegramSurfaceBase


@dataclass(frozen=True, slots=True)
class PersistedDraft:
    """The part of a draft that has to outlive the process that made it."""

    exchange_id: str
    message_id: int
    reply_to: int | None
    sealed_chunks: int


class DraftStore(Protocol):
    """Port over the drafts of one chat."""

    async def remember(self, chat_id: int, draft: PersistedDraft) -> None:
        """Record (or replace) where this exchange's answer is being written."""
        ...

    async def load(self, chat_id: int) -> list[PersistedDraft]:
        """Every remembered draft of the chat, for a bridge that just attached."""
        ...

    async def forget(self, exchange_id: str) -> None:
        """Drop the draft; the answer is finished and the message is final."""
        ...


class DraftRow(TelegramSurfaceBase):
    """One in-flight answer's message, keyed by the exchange it belongs to."""

    __tablename__ = "telegram_drafts"

    exchange_id: Mapped[str] = mapped_column(String, primary_key=True)
    # indexed: a bridge attaching loads every draft of its chat at once
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[int] = mapped_column(Integer)
    reply_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sealed_chunks: Mapped[int] = mapped_column(Integer, default=0)


class SqlAlchemyDraftStore:
    """Draft store on the Telegram surface's own database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def remember(self, chat_id: int, draft: PersistedDraft) -> None:
        """Record (or replace) where this exchange's answer is being written."""
        async with self._session_factory() as session:
            row = await session.get(DraftRow, draft.exchange_id)
            if row is None:
                row = DraftRow(exchange_id=draft.exchange_id, chat_id=chat_id)
                session.add(row)
            row.chat_id = chat_id
            row.message_id = draft.message_id
            row.reply_to = draft.reply_to
            row.sealed_chunks = draft.sealed_chunks
            await session.commit()

    async def load(self, chat_id: int) -> list[PersistedDraft]:
        """Every remembered draft of the chat, for a bridge that just attached."""
        async with self._session_factory() as session:
            rows = await session.scalars(select(DraftRow).where(DraftRow.chat_id == chat_id))
            return [
                PersistedDraft(
                    exchange_id=row.exchange_id,
                    message_id=row.message_id,
                    reply_to=row.reply_to,
                    sealed_chunks=row.sealed_chunks,
                )
                for row in rows.all()
            ]

    async def forget(self, exchange_id: str) -> None:
        """Drop the draft; the answer is finished and the message is final."""
        async with self._session_factory() as session:
            await session.execute(delete(DraftRow).where(DraftRow.exchange_id == exchange_id))
            await session.commit()
