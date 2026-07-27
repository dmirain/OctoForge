"""SQL stores of the dialogs module: the dialog registry and the message log.

Sessions come from an injected `async_sessionmaker`; ORM rows map to domain
objects (`Dialog`, `ChatMessage` from the shared kernel) at the boundary.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.dialogs.api import DialogNotFoundError, MessageStats, MessageStatsList
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.domain import ChatMessage, Dialog, MessageRole, ToolCall
from octoforge_core.llm.usage import Usage
from octoforge_core.time import utc_now

# A lost seq race (concurrent writers reading the same MAX(seq) before either
# commits) surfaces as a unique (dialog_id, seq) violation; retried with a
# freshly recomputed seq rather than propagated. Bounded so a genuine
# duplicate client_message_id still raises instead of looping forever.
MESSAGE_SEQ_RETRY_ATTEMPTS = 5


class SqlAlchemyDialogRepository:
    """Dialogs keyed by the unique (user_id, channel) pair."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_or_create(self, user_id: str, channel: str) -> Dialog:
        """Return the dialog for (user_id, channel), creating it on first contact."""
        async with self._session_factory() as session:
            row = await self._find_row(session, user_id, channel)
            if row is None:
                row = DialogRow(id=uuid.uuid4().hex, user_id=user_id, channel=channel)
                session.add(row)
                await session.commit()
            return _to_dialog(row)

    async def get(self, dialog_id: str) -> Dialog:
        """Return the dialog by id or raise DialogNotFoundError."""
        async with self._session_factory() as session:
            row = await session.get(DialogRow, dialog_id)
            if row is None:
                raise DialogNotFoundError(dialog_id)
            return _to_dialog(row)

    async def list_user_ids_by_channel(self, channel: str) -> list[str]:
        """Return the user ids that have a dialog on the given channel."""
        async with self._session_factory() as session:
            result = await session.scalars(
                select(DialogRow.user_id).where(DialogRow.channel == channel)
            )
            return list(result.all())

    async def list_by_channel(self, channel: str) -> list[Dialog]:
        """Return the full dialogs of the given channel (activity timestamps included)."""
        async with self._session_factory() as session:
            result = await session.scalars(select(DialogRow).where(DialogRow.channel == channel))
            return [_to_dialog(row) for row in result.all()]

    @staticmethod
    async def _find_row(session: AsyncSession, user_id: str, channel: str) -> DialogRow | None:
        result = await session.scalars(
            select(DialogRow).where(DialogRow.user_id == user_id, DialogRow.channel == channel)
        )
        return result.first()


class SqlAlchemyMessageRepository:
    """Ordered message log of a dialog; seq grows monotonically per dialog."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(
        self,
        dialog_id: str,
        message: ChatMessage,
        usage: Usage | None = None,
        client_message_id: str | None = None,
    ) -> str:
        """Append a message assigning it the next seq within the dialog; return the row id.

        The seq is computed from the current max inside the INSERT statement;
        two concurrent writers (the actor and a process pump, each on its own
        session) can still both read the same max before either commits. The
        loser's unique (dialog_id, seq) violation is retried with a freshly
        recomputed seq (see `MESSAGE_SEQ_RETRY_ATTEMPTS`) rather than lost.
        `usage` (provider token accounting) is stored only on assistant
        messages. `client_message_id` is the idempotency key of client
        submits; the unique (dialog_id, client_message_id) constraint rejects
        duplicates (raised on the final attempt, not silently retried away).
        """
        for attempt in range(MESSAGE_SEQ_RETRY_ATTEMPTS):
            row_id = uuid.uuid4().hex
            async with self._session_factory() as session:
                next_seq = (
                    select(func.coalesce(func.max(MessageRow.seq), 0) + 1)
                    .where(MessageRow.dialog_id == dialog_id)
                    .scalar_subquery()
                )
                await session.execute(
                    insert(MessageRow).values(
                        id=row_id,
                        dialog_id=dialog_id,
                        seq=next_seq,
                        role=message.role.value,
                        content=message.content,
                        tool_calls=_tool_calls_to_json(message.tool_calls),
                        tool_call_id=message.tool_call_id,
                        client_message_id=client_message_id,
                        prompt_tokens=usage.prompt_tokens if usage is not None else None,
                        completion_tokens=usage.completion_tokens if usage is not None else None,
                        task_id=message.task_id,
                    )
                )
                dialog = await session.get(DialogRow, dialog_id)
                if dialog is not None:
                    dialog.updated_at = utc_now()
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    if attempt == MESSAGE_SEQ_RETRY_ATTEMPTS - 1:
                        raise
                    continue
                return row_id
        raise AssertionError("unreachable: the final attempt either returns or raises")

    async def append_pair(
        self,
        dialog_id: str,
        first: ChatMessage,
        second: ChatMessage,
    ) -> None:
        """Append two messages in one transaction, with consecutive seq values.

        Used for indivisible narrative pairs (a salvaged partial answer and
        its INTERRUPTED_NOTE): a reader snapshot between two separate commits
        could otherwise see the first message without the second. Each INSERT
        keeps the atomic seq subquery, so the pair also stays consistent
        against concurrent writers within the transaction; a losing unique
        (dialog_id, seq) violation against another transaction is retried
        with a freshly recomputed pair rather than lost (see
        `MESSAGE_SEQ_RETRY_ATTEMPTS`).
        """
        for attempt in range(MESSAGE_SEQ_RETRY_ATTEMPTS):
            async with self._session_factory() as session:
                next_seq = (
                    select(func.coalesce(func.max(MessageRow.seq), 0) + 1)
                    .where(MessageRow.dialog_id == dialog_id)
                    .scalar_subquery()
                )
                for message in (first, second):
                    await session.execute(
                        insert(MessageRow).values(
                            id=uuid.uuid4().hex,
                            dialog_id=dialog_id,
                            seq=next_seq,
                            role=message.role.value,
                            content=message.content,
                            tool_calls=_tool_calls_to_json(message.tool_calls),
                            tool_call_id=message.tool_call_id,
                            task_id=message.task_id,
                        )
                    )
                dialog = await session.get(DialogRow, dialog_id)
                if dialog is not None:
                    dialog.updated_at = utc_now()
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    if attempt == MESSAGE_SEQ_RETRY_ATTEMPTS - 1:
                        raise
                    continue
                return

    async def find_by_client_id(self, dialog_id: str, client_message_id: str) -> bool:
        """Return True when a message with this idempotency key already exists."""
        async with self._session_factory() as session:
            value = await session.scalar(
                select(MessageRow.id)
                .where(
                    MessageRow.dialog_id == dialog_id,
                    MessageRow.client_message_id == client_message_id,
                )
                .limit(1)
            )
            return value is not None

    async def list_after(self, dialog_id: str, after_seq: int) -> list[ChatMessage]:
        """Return the messages with seq strictly above `after_seq`, ordered by seq.

        The actor's initial narrative load: only the hot slice past the
        compaction boundary lives in memory — older history is reachable
        through summaries and history_search. (Defined above `list`: that
        method shadows the builtin in the class scope, breaking annotations
        below it.)
        """
        async with self._session_factory() as session:
            result = await session.scalars(
                select(MessageRow)
                .where(MessageRow.dialog_id == dialog_id, MessageRow.seq > after_seq)
                .order_by(MessageRow.seq)
            )
            return [_to_chat_message(row) for row in result.all()]

    async def list(self, dialog_id: str) -> list[ChatMessage]:
        """Return the dialog messages ordered by seq."""
        async with self._session_factory() as session:
            result = await session.scalars(
                select(MessageRow).where(MessageRow.dialog_id == dialog_id).order_by(MessageRow.seq)
            )
            return [_to_chat_message(row) for row in result.all()]

    async def stats_by_channel(self, channel: str) -> MessageStatsList:
        """Return per-user message counters of the channel, one entry per dialog owner."""
        async with self._session_factory() as session:
            statement = (
                select(
                    DialogRow.user_id,
                    func.count(MessageRow.id),
                    func.coalesce(func.sum(func.length(MessageRow.content)), 0),
                )
                .join(DialogRow, MessageRow.dialog_id == DialogRow.id)
                .where(DialogRow.channel == channel)
                .group_by(DialogRow.user_id)
            )
            rows = (await session.execute(statement)).all()
            return [
                MessageStats(user_id=user_id, message_count=count, total_chars=total)
                for user_id, count, total in rows
            ]


def _to_dialog(row: DialogRow) -> Dialog:
    return Dialog(
        id=row.id,
        user_id=row.user_id,
        channel=row.channel,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_chat_message(row: MessageRow) -> ChatMessage:
    return ChatMessage(
        role=MessageRole(row.role),
        content=row.content,
        tool_calls=_tool_calls_from_json(row.tool_calls),
        tool_call_id=row.tool_call_id,
        task_id=row.task_id,
        id=row.id,
    )


def _tool_calls_to_json(tool_calls: tuple[ToolCall, ...]) -> list[dict[str, Any]] | None:
    if not tool_calls:
        return None
    return [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in tool_calls]


def _tool_calls_from_json(raw: list[dict[str, Any]] | None) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    return tuple(
        ToolCall(id=str(item["id"]), name=str(item["name"]), arguments=dict(item["arguments"]))
        for item in raw
    )
