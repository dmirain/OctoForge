"""SQL stores of the dialogs module: the dialog registry and the message log.

Sessions come from an injected `async_sessionmaker`; ORM rows map to domain
objects (`Dialog`, `ChatMessage` from the shared kernel) at the boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, case, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.dml import ReturningInsert

from octoforge_core.context.api import NO_COMPACTED_SEQ
from octoforge_core.context.models import SummaryRow
from octoforge_core.dialogs.api import (
    LIVE_EXCHANGE_STATUSES,
    TITLE_MAX_LENGTH,
    DialogClaim,
    DialogClaimList,
    DialogNotFoundError,
    Exchange,
    ExchangeList,
    ExchangeNotFoundError,
    ExchangeStatus,
    MessageStats,
    MessageStatsList,
)
from octoforge_core.dialogs.models import DialogClaimRow, DialogRow, ExchangeRow, MessageRow
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

# The first claim on a dialog. Generations start at 1 so that "no row" and
# "never claimed" stay the same thing, told apart by None rather than by 0.
FIRST_GENERATION = 1


def _claim_upsert(
    session: AsyncSession, dialog_id: str, owner: str, now: datetime
) -> ReturningInsert[tuple[int]]:
    """INSERT-or-bump for one claim row, in this session's dialect.

    `ON CONFLICT DO UPDATE` is spelled per dialect in SQLAlchemy even though
    both backends support it, so the statement is built where the bind is
    known rather than at import time. `RETURNING generation` is what makes it
    one round trip: the new generation comes back from the write itself.
    """
    insert_for_dialect = (
        postgresql_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    )
    statement = insert_for_dialect(DialogClaimRow).values(
        dialog_id=dialog_id,
        owner=owner,
        generation=FIRST_GENERATION,
        heartbeat_at=now,
    )
    return statement.on_conflict_do_update(
        index_elements=[DialogClaimRow.dialog_id],
        set_={
            "owner": statement.excluded.owner,
            "generation": DialogClaimRow.generation + 1,
            "heartbeat_at": statement.excluded.heartbeat_at,
        },
    ).returning(DialogClaimRow.generation)


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

    async def last_activity_by_channel(self, channel: str) -> dict[str, datetime]:
        """When each person of the channel last had a message, by person.

        The dialog row used to carry this as `updated_at`, bumped on every
        append — two writes per turn to serve two operator listings. Reading
        it from the messages themselves costs nothing on the answer path and
        cannot drift from what actually happened.
        """
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(DialogRow.user_id, func.max(MessageRow.created_at))
                    .join(DialogRow, MessageRow.dialog_id == DialogRow.id)
                    .where(DialogRow.channel == channel)
                    .group_by(DialogRow.user_id)
                )
            ).all()
        return {user_id: last for user_id, last in rows if last is not None}

    async def list_hot_slice(self, dialog_id: str) -> list[ChatMessage]:
        """The narrative's hot slice: everything past the compaction boundary.

        The boundary is a subquery rather than a separate lookup. It used to
        be two calls — ask the summaries for the highest covered seq, then ask
        the messages for what comes after — and the first answer was never
        needed for anything but the second question.
        """
        boundary = (
            select(func.coalesce(func.max(SummaryRow.seq_to), NO_COMPACTED_SEQ))
            .where(SummaryRow.dialog_id == dialog_id)
            .scalar_subquery()
        )
        async with self._session_factory() as session:
            result = await session.scalars(
                select(MessageRow)
                .where(MessageRow.dialog_id == dialog_id, MessageRow.seq > boundary)
                .order_by(MessageRow.seq)
            )
            return [_to_chat_message(row) for row in result.all()]

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
        """Attach a stored message to the exchange it belongs to.

        A bare UPDATE rather than read-modify-write: the row is not needed,
        only written, and the extra SELECT is a round trip that costs nothing
        beside the database and 11 ms across a tunnel to one. A message that
        is not there is still not an error here — same as before.
        """
        async with self._session_factory() as session:
            await session.execute(
                update(MessageRow)
                .where(MessageRow.id == message_id)
                .values(exchange_id=exchange_id)
            )
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
            await session.execute(
                update(ExchangeRow)
                .where(ExchangeRow.id == exchange_id)
                .values(updated_at=utc_now())
            )
            await session.commit()

    async def set_title(self, exchange_id: str, title: str) -> None:
        """Rename the exchange; a missing row is a no-op (it may have been deleted)."""
        async with self._session_factory() as session:
            await session.execute(
                update(ExchangeRow)
                .where(ExchangeRow.id == exchange_id)
                .values(title=title[:TITLE_MAX_LENGTH])
            )
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

    async def list_stranded_dialog_ids(self) -> list[str]:
        """Dialog ids holding an IN_PROGRESS or an OPEN-and-unowned exchange."""
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ExchangeRow.dialog_id)
                .where(
                    (ExchangeRow.status == ExchangeStatus.IN_PROGRESS.value)
                    | (
                        (ExchangeRow.status == ExchangeStatus.OPEN.value)
                        & ExchangeRow.owner_task_id.is_(None)
                    )
                )
                .distinct()
            )
            return list(result.all())

    async def reopen_in_progress(self, dialog_id: str) -> int:
        """Reset the dialog's IN_PROGRESS exchanges to OPEN; return how many."""
        async with self._session_factory() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(ExchangeRow)
                    .where(
                        ExchangeRow.dialog_id == dialog_id,
                        ExchangeRow.status == ExchangeStatus.IN_PROGRESS.value,
                    )
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
        """Write the exchange's status; raise if there is no such exchange.

        One UPDATE, not a read followed by a write: `rowcount` answers the
        same question the SELECT was asked (does this exchange exist), and
        the condition rides in the WHERE clause instead of a round trip.
        """
        async with self._session_factory() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(ExchangeRow)
                    .where(ExchangeRow.id == exchange_id)
                    .values(
                        status=status.value,
                        owner_task_id=owner_task_id,
                        pending_question=pending_question,
                    )
                ),
            )
            if result.rowcount == 0:
                raise ExchangeNotFoundError(exchange_id)
            await session.commit()

    async def settle_owned(
        self,
        exchange_id: str,
        owner_task_id: str,
        status: ExchangeStatus,
        *,
        keep_if_awaiting: bool = False,
    ) -> Exchange | None:
        """Move the exchange to `status`, but only while `owner_task_id` still owns it.

        Returns the settled exchange, or None when nothing was written — the
        exchange is gone, it changed hands, or it is parked on the user's
        answer and this settle was told to leave that alone.

        The guard belongs in the WHERE clause, not in a preceding SELECT.
        Read-then-write left a window in which a follow-up could take the
        exchange over between the check and the write, and the write would
        then clobber a live run's ownership — the very thing the check was
        there to prevent. One statement closes it, and saves a round trip
        while doing so.

        `keep_if_awaiting` is the AWAITING_USER carve-out: a run that asked
        the user something leaves the obligation open, and overwriting it
        would drop the pending question. A predicate rather than a branch,
        because deciding it in Python is what needed the read.
        """
        conditions = [
            ExchangeRow.id == exchange_id,
            ExchangeRow.owner_task_id == owner_task_id,
        ]
        if keep_if_awaiting:
            conditions.append(ExchangeRow.status != ExchangeStatus.AWAITING_USER.value)
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    update(ExchangeRow)
                    .where(*conditions)
                    .values(status=status.value, owner_task_id=None, pending_question=None)
                    .returning(ExchangeRow)
                )
            ).first()
            if row is None:
                return None
            exchange = _to_exchange(row)
            await session.commit()
        return exchange

    async def delete_for_dialog(self, dialog_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(delete(ExchangeRow).where(ExchangeRow.dialog_id == dialog_id))
            await session.commit()


class SqlAlchemyClaimRepository:
    """Dialog ownership: which process runs which actor.

    Claiming is one upsert. The identity of a claim is the PAIR
    (owner, generation), never the number alone: two processes racing to take
    the same dialog may both land on the same generation, but only one owner
    survives in the row, so the other fails its next check and stands down.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(self, dialog_id: str, owner: str) -> DialogClaim:
        """Take the dialog for `owner`, bumping its generation.

        One statement: INSERT the first claim, or bump the existing one on
        conflict. It used to be a SELECT followed by an INSERT-or-UPDATE, with
        a retry for the race where somebody inserted the first claim in
        between — the upsert has no such window, so both the extra round trip
        and the retry loop are gone. The generation is computed in SQL
        (`generation + 1`), not in Python, so it cannot be computed from a
        value that has already moved.
        """
        now = utc_now()
        async with self._session_factory() as session:
            generation = await session.scalar(_claim_upsert(session, dialog_id, owner, now))
            await session.commit()
        return DialogClaim(
            dialog_id=dialog_id,
            owner=owner,
            generation=int(generation) if generation is not None else FIRST_GENERATION,
            heartbeat_at=now,
        )

    async def heartbeat(self, claims: DialogClaimList) -> frozenset[str]:
        """Refresh the given claims; return the ids still held by their owner."""
        if not claims:
            return frozenset()
        held = {claim.dialog_id: claim for claim in claims}
        now = utc_now()
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        DialogClaimRow.dialog_id, DialogClaimRow.owner, DialogClaimRow.generation
                    ).where(DialogClaimRow.dialog_id.in_(held))
                )
            ).all()
            kept = frozenset(
                dialog_id
                for dialog_id, owner, generation in rows
                if held[dialog_id].owner == owner and held[dialog_id].generation == generation
            )
            if kept:
                await session.execute(
                    update(DialogClaimRow)
                    .where(DialogClaimRow.dialog_id.in_(kept))
                    .values(heartbeat_at=now)
                )
                await session.commit()
            return kept

    async def release(self, dialog_id: str, owner: str, generation: int) -> None:
        """Drop the claim if it is still this exact one; otherwise a no-op."""
        async with self._session_factory() as session:
            await session.execute(
                delete(DialogClaimRow).where(
                    DialogClaimRow.dialog_id == dialog_id,
                    DialogClaimRow.owner == owner,
                    DialogClaimRow.generation == generation,
                )
            )
            await session.commit()

    async def held_elsewhere(
        self, dialog_ids: frozenset[str], owner: str, stale_before: datetime
    ) -> frozenset[str]:
        """Of `dialog_ids`, those a different owner holds with a fresh heartbeat."""
        if not dialog_ids:
            return frozenset()
        async with self._session_factory() as session:
            result = await session.scalars(
                select(DialogClaimRow.dialog_id).where(
                    DialogClaimRow.dialog_id.in_(dialog_ids),
                    DialogClaimRow.owner != owner,
                    DialogClaimRow.heartbeat_at >= stale_before,
                )
            )
            return frozenset(result.all())

    async def current_generation(self, dialog_id: str) -> int | None:
        """The stored generation, or None when nobody holds the dialog."""
        async with self._session_factory() as session:
            generation: int | None = await session.scalar(
                select(DialogClaimRow.generation).where(DialogClaimRow.dialog_id == dialog_id)
            )
            return generation

    async def delete_for_dialog(self, dialog_id: str) -> None:
        """Drop the dialog's claim (admin dialog deletion)."""
        async with self._session_factory() as session:
            await session.execute(
                delete(DialogClaimRow).where(DialogClaimRow.dialog_id == dialog_id)
            )
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
