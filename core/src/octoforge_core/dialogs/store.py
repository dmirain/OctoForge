"""SQL stores of the dialogs module: the dialog registry and the message log.

Sessions come from an injected `async_sessionmaker`; ORM rows map to domain
objects (`Dialog`, `ChatMessage` from the shared kernel) at the boundary.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, case, delete, func, insert, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.dialogs.api import (
    LIVE_EXCHANGE_STATUSES,
    TITLE_MAX_LENGTH,
    DialogNotFoundError,
    Exchange,
    ExchangeList,
    ExchangeNotFoundError,
    ExchangeStatus,
    MessageStats,
    MessageStatsList,
)
from octoforge_core.dialogs.models import DialogRow, ExchangeRow, MessageRow
from octoforge_core.domain import (
    Attachment,
    AttachmentKind,
    ChatMessage,
    Dialog,
    MessageKind,
    MessageRole,
    ToolCall,
)
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

    async def delete(self, dialog_id: str) -> None:
        """Delete the dialog and its message log in one transaction."""
        async with self._session_factory() as session:
            row = await session.get(DialogRow, dialog_id)
            if row is None:
                raise DialogNotFoundError(dialog_id)
            await session.execute(delete(MessageRow).where(MessageRow.dialog_id == dialog_id))
            await session.delete(row)
            await session.commit()

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
                # The INSERT is inside the try, not just the commit: a Core
                # insert goes to the server immediately, so when the winning row
                # is ALREADY committed the unique violation surfaces here rather
                # than at commit time. Guarding only the commit made the retry
                # work or not depending on which writer got there first — a
                # one-in-ten lost message under concurrent appends to one
                # dialog, which the exchange model makes routine.
                try:
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
                            completion_tokens=(
                                usage.completion_tokens if usage is not None else None
                            ),
                            task_id=message.task_id,
                            exchange_id=message.exchange_id,
                            kind=_kind_to_column(message.kind),
                            attachments=_attachments_to_json(message.attachments),
                        )
                    )
                    dialog = await session.get(DialogRow, dialog_id)
                    if dialog is not None:
                        dialog.updated_at = utc_now()
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
                # Same reason as in `append`: the violation can surface on the
                # INSERT itself, so the retry has to cover it.
                try:
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
                                exchange_id=message.exchange_id,
                                kind=_kind_to_column(message.kind),
                                attachments=_attachments_to_json(message.attachments),
                            )
                        )
                    dialog = await session.get(DialogRow, dialog_id)
                    if dialog is not None:
                        dialog.updated_at = utc_now()
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

    async def set_exchange(self, message_id: str, exchange_id: str) -> None:
        """Attach a stored message to the exchange it belongs to."""
        async with self._session_factory() as session:
            row = await session.get(MessageRow, message_id)
            if row is not None:
                row.exchange_id = exchange_id
                await session.commit()

    async def stats_by_channel(self, channel: str) -> MessageStatsList:
        """Return per-user message counters of the channel, split by author.

        Conditional aggregation in one pass: the user's own messages and the
        agent's answers are counted separately (other roles are neither).
        """

        def role_count(role: MessageRole) -> ColumnElement[int]:
            return func.count(case((MessageRow.role == role.value, 1)))

        def role_chars(role: MessageRole) -> ColumnElement[int]:
            return func.coalesce(
                func.sum(case((MessageRow.role == role.value, func.length(MessageRow.content)))),
                0,
            )

        async with self._session_factory() as session:
            statement = (
                select(
                    DialogRow.user_id,
                    role_count(MessageRole.USER),
                    role_chars(MessageRole.USER),
                    role_count(MessageRole.ASSISTANT),
                    role_chars(MessageRole.ASSISTANT),
                )
                .join(DialogRow, MessageRow.dialog_id == DialogRow.id)
                .where(DialogRow.channel == channel)
                .group_by(DialogRow.user_id)
            )
            rows = (await session.execute(statement)).all()
            return [
                MessageStats(
                    user_id=user_id,
                    user_messages=user_count,
                    user_chars=user_chars,
                    agent_messages=agent_count,
                    agent_chars=agent_chars,
                )
                for user_id, user_count, user_chars, agent_count, agent_chars in rows
            ]


class SqlAlchemyExchangeRepository:
    """Exchanges of a dialog: the durable obligation state."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        dialog_id: str,
        title: str,
        owner_task_id: str | None = None,
        status: ExchangeStatus | None = None,
    ) -> Exchange:
        """Open a new exchange; IN_PROGRESS when an owner is given, OPEN otherwise."""
        resolved = status or (ExchangeStatus.IN_PROGRESS if owner_task_id else ExchangeStatus.OPEN)
        async with self._session_factory() as session:
            row = ExchangeRow(
                id=uuid.uuid4().hex,
                dialog_id=dialog_id,
                status=resolved.value,
                title=title[:TITLE_MAX_LENGTH],
                owner_task_id=owner_task_id,
            )
            session.add(row)
            await session.commit()
            return _to_exchange(row)

    async def find_collecting(self, dialog_id: str) -> Exchange | None:
        """Return the dialog's collecting exchange, None when there is none."""
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ExchangeRow)
                .where(
                    ExchangeRow.dialog_id == dialog_id,
                    ExchangeRow.status == ExchangeStatus.COLLECTING.value,
                )
                .order_by(ExchangeRow.created_at)
                .limit(1)
            )
            row = result.first()
            return _to_exchange(row) if row is not None else None

    async def list_stale_collecting(self, quiet_seconds: float) -> ExchangeList:
        """Collecting exchanges untouched for longer than `quiet_seconds`."""
        threshold = utc_now() - timedelta(seconds=quiet_seconds)
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ExchangeRow)
                .where(
                    ExchangeRow.status == ExchangeStatus.COLLECTING.value,
                    ExchangeRow.updated_at < threshold,
                )
                .order_by(ExchangeRow.updated_at)
            )
            return [_to_exchange(row) for row in result.all()]

    async def touch(self, exchange_id: str) -> None:
        """Bump `updated_at`; a missing row is a no-op (it may have been deleted)."""
        async with self._session_factory() as session:
            row = await session.get(ExchangeRow, exchange_id)
            if row is None:
                return
            row.updated_at = utc_now()
            await session.commit()

    async def set_title(self, exchange_id: str, title: str) -> None:
        """Rename the exchange; a missing row is a no-op (it may have been deleted)."""
        async with self._session_factory() as session:
            row = await session.get(ExchangeRow, exchange_id)
            if row is None:
                return
            row.title = title[:TITLE_MAX_LENGTH]
            await session.commit()

    async def get(self, exchange_id: str) -> Exchange:
        async with self._session_factory() as session:
            row = await session.get(ExchangeRow, exchange_id)
            if row is None:
                raise ExchangeNotFoundError(exchange_id)
            return _to_exchange(row)

    async def list_live(self, dialog_id: str) -> ExchangeList:
        """Return the dialog's non-terminal exchanges, oldest first."""
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ExchangeRow)
                .where(
                    ExchangeRow.dialog_id == dialog_id,
                    ExchangeRow.status.in_([status.value for status in LIVE_EXCHANGE_STATUSES]),
                )
                .order_by(ExchangeRow.created_at)
            )
            return [_to_exchange(row) for row in result.all()]

    async def list_unowned_open(self, dialog_id: str | None = None) -> ExchangeList:
        """OPEN exchanges without an owner, oldest first (None: all dialogs)."""
        query = (
            select(ExchangeRow)
            .where(
                ExchangeRow.status == ExchangeStatus.OPEN.value,
                ExchangeRow.owner_task_id.is_(None),
            )
            .order_by(ExchangeRow.created_at)
        )
        if dialog_id is not None:
            query = query.where(ExchangeRow.dialog_id == dialog_id)
        async with self._session_factory() as session:
            result = await session.scalars(query)
            return [_to_exchange(row) for row in result.all()]

    async def reopen_in_progress(self) -> int:
        """Reset every IN_PROGRESS exchange to OPEN; return how many (startup)."""
        async with self._session_factory() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(ExchangeRow)
                    .where(ExchangeRow.status == ExchangeStatus.IN_PROGRESS.value)
                    .values(status=ExchangeStatus.OPEN.value, owner_task_id=None)
                ),
            )
            await session.commit()
            return result.rowcount or 0

    async def set_status(
        self,
        exchange_id: str,
        status: ExchangeStatus,
        owner_task_id: str | None = None,
        pending_question: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(ExchangeRow, exchange_id)
            if row is None:
                raise ExchangeNotFoundError(exchange_id)
            row.status = status.value
            row.owner_task_id = owner_task_id
            row.pending_question = pending_question
            await session.commit()

    async def delete_for_dialog(self, dialog_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(delete(ExchangeRow).where(ExchangeRow.dialog_id == dialog_id))
            await session.commit()


def _to_exchange(row: ExchangeRow) -> Exchange:
    return Exchange(
        id=row.id,
        dialog_id=row.dialog_id,
        status=ExchangeStatus(row.status),
        title=row.title,
        owner_task_id=row.owner_task_id,
        pending_question=row.pending_question,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


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
        kind=MessageKind(row.kind) if row.kind else MessageKind.OWN,
        attachments=_attachments_from_json(row.attachments),
        id=row.id,
        exchange_id=row.exchange_id,
    )


def _attachments_to_json(items: tuple[Attachment, ...]) -> list[dict[str, Any]] | None:
    """Store attachments only when there are any; NULL keeps legacy rows clean."""
    if not items:
        return None
    return [{"kind": item.kind.value, "ref": item.ref} for item in items]


def _attachments_from_json(raw: list[dict[str, Any]] | None) -> tuple[Attachment, ...]:
    if not raw:
        return ()
    return tuple(
        Attachment(kind=AttachmentKind(item["kind"]), ref=str(item["ref"])) for item in raw
    )


def _kind_to_column(kind: MessageKind) -> str | None:
    """Store only the exceptional kind; NULL keeps legacy rows meaningful."""
    return None if kind is MessageKind.OWN else kind.value


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
