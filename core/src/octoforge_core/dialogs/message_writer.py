"""Race-safe atomic appends to a dialog's ordered message log."""

import uuid

from sqlalchemy import Insert, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import ScalarSelect

from octoforge_core.db.unit_of_work import (
    in_unit_of_work,
    unit_has_writes,
    write_session,
)
from octoforge_core.dialogs.message_insert import MessageRowInput, message_insert
from octoforge_core.dialogs.models import MessageRow
from octoforge_core.dialogs.requests import MessageAppend
from octoforge_core.domain import ChatMessage

MESSAGE_SEQ_RETRY_ATTEMPTS = 5


class MessageWriter:
    """Assign monotonic seq values and retry only lost seq races."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def append(self, request: MessageAppend) -> str:
        for attempt in range(MESSAGE_SEQ_RETRY_ATTEMPTS):
            row_id = uuid.uuid4().hex
            statement = message_insert(
                MessageRowInput(row_id, request, _next_seq(request.dialog_id))
            )
            try:
                await self._insert_attempt((statement,))
            except IntegrityError:
                if attempt == MESSAGE_SEQ_RETRY_ATTEMPTS - 1:
                    raise
                continue
            return row_id
        raise AssertionError("unreachable: final message append attempt returns or raises")

    async def append_pair(
        self,
        dialog_id: str,
        first: ChatMessage,
        second: ChatMessage,
    ) -> None:
        for attempt in range(MESSAGE_SEQ_RETRY_ATTEMPTS):
            next_seq = _next_seq(dialog_id)
            statements = tuple(
                message_insert(
                    MessageRowInput(
                        uuid.uuid4().hex,
                        MessageAppend(dialog_id, message),
                        next_seq,
                    )
                )
                for message in (first, second)
            )
            try:
                await self._insert_attempt(statements)
            except IntegrityError:
                if attempt == MESSAGE_SEQ_RETRY_ATTEMPTS - 1:
                    raise
                continue
            return

    async def _insert_attempt(self, statements: tuple[Insert, ...]) -> None:
        async with write_session(self._sessions) as session:
            if in_unit_of_work() and session.get_bind().dialect.name != "sqlite":
                if unit_has_writes(session):
                    async with session.begin_nested():
                        await _execute_all(session, statements)
                    return
                try:
                    await _execute_all(session, statements)
                except IntegrityError:
                    await session.rollback()
                    raise
                return
            await _execute_all(session, statements)


def _next_seq(dialog_id: str) -> ScalarSelect[int]:
    return (
        select(func.coalesce(func.max(MessageRow.seq), 0) + 1)
        .where(MessageRow.dialog_id == dialog_id)
        .scalar_subquery()
    )


async def _execute_all(session: AsyncSession, statements: tuple[Insert, ...]) -> None:
    for statement in statements:
        await session.execute(statement)
