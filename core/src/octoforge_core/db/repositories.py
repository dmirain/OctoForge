"""Repositories and stores mapping between ORM rows and domain objects."""

import uuid
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.errors import DialogNotFoundError
from octoforge_core.db.models import DialogRow, MessageRow, TaskRow
from octoforge_core.domain import ChatMessage, Dialog, MessageRole, ToolCall
from octoforge_core.tasks.errors import TaskNotFoundError
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.time import utc_now


class DialogRepository:
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

    @staticmethod
    async def _find_row(session: AsyncSession, user_id: str, channel: str) -> DialogRow | None:
        result = await session.scalars(
            select(DialogRow).where(DialogRow.user_id == user_id, DialogRow.channel == channel)
        )
        return result.first()


class MessageRepository:
    """Ordered message log of a dialog; seq grows monotonically per dialog."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(self, dialog_id: str, message: ChatMessage) -> None:
        """Append a message assigning it the next seq within the dialog.

        The seq is computed inside the INSERT statement, so concurrent writers
        (the actor and the process pumps) cannot assign the same seq.
        """
        async with self._session_factory() as session:
            next_seq = (
                select(func.coalesce(func.max(MessageRow.seq), 0) + 1)
                .where(MessageRow.dialog_id == dialog_id)
                .scalar_subquery()
            )
            await session.execute(
                insert(MessageRow).values(
                    id=uuid.uuid4().hex,
                    dialog_id=dialog_id,
                    seq=next_seq,
                    role=message.role.value,
                    content=message.content,
                    tool_calls=_tool_calls_to_json(message.tool_calls),
                    tool_call_id=message.tool_call_id,
                )
            )
            dialog = await session.get(DialogRow, dialog_id)
            if dialog is not None:
                dialog.updated_at = utc_now()
            await session.commit()

    async def list(self, dialog_id: str) -> list[ChatMessage]:
        """Return the dialog messages ordered by seq."""
        async with self._session_factory() as session:
            result = await session.scalars(
                select(MessageRow).where(MessageRow.dialog_id == dialog_id).order_by(MessageRow.seq)
            )
            return [_to_chat_message(row) for row in result.all()]


class SqlAlchemyTaskStore:
    """SQLAlchemy-backed implementation of the TaskStore port."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, task: Task) -> None:
        async with self._session_factory() as session:
            session.add(_to_task_row(task))
            await session.commit()

    async def get(self, task_id: str) -> Task:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            return _to_task(row)

    async def list(self, dialog_id: str) -> list[Task]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(TaskRow).where(TaskRow.dialog_id == dialog_id).order_by(TaskRow.created_at)
            )
            return [_to_task(row) for row in result.all()]

    async def mark_running(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = utc_now()
        await self._update(task)

    async def mark_done(self, task: Task, result: str) -> None:
        task.status = TaskStatus.DONE
        task.result = result
        task.finished_at = utc_now()
        await self._update(task)

    async def mark_failed(self, task: Task, error: str) -> None:
        task.status = TaskStatus.FAILED
        task.error = error
        task.finished_at = utc_now()
        await self._update(task)

    async def cancel(self, task_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            row.status = TaskStatus.CANCELLED.value
            row.finished_at = utc_now()
            await session.commit()

    async def is_cancelled(self, task_id: str) -> bool:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task_id)
            return row is not None and row.status == TaskStatus.CANCELLED.value

    async def count_active(self, dialog_id: str) -> int:
        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count(TaskRow.id)).where(
                    TaskRow.dialog_id == dialog_id,
                    TaskRow.status.in_((TaskStatus.PENDING.value, TaskStatus.RUNNING.value)),
                )
            )
            return int(count or 0)

    async def mark_delivered(self, task_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            row.result_delivered = True
            await session.commit()

    async def _update(self, task: Task) -> None:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task.id)
            if row is None:
                raise TaskNotFoundError(task.id)
            row.status = task.status.value
            row.result = task.result
            row.error = task.error
            row.started_at = task.started_at
            row.finished_at = task.finished_at
            await session.commit()


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


def _to_task_row(task: Task) -> TaskRow:
    return TaskRow(
        id=task.id,
        dialog_id=task.dialog_id,
        user_id=task.user_id,
        channel=task.channel,
        kind=task.kind.value,
        title=task.title,
        input=task.input,
        status=task.status.value,
        result=task.result,
        error=task.error,
        result_delivered=task.result_delivered,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
    )


def _to_task(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        dialog_id=row.dialog_id,
        user_id=row.user_id,
        channel=row.channel,
        kind=TaskKind(row.kind),
        title=row.title,
        input=row.input,
        status=TaskStatus(row.status),
        result=row.result,
        error=row.error,
        result_delivered=row.result_delivered,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )
