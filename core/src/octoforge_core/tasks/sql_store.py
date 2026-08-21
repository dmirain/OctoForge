"""SQL TaskStore with single-query recovery and terminal updates."""

from typing import Any, cast

from sqlalchemy import Select, delete, or_, select, update
from sqlalchemy import and_ as sa_and
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.tasks._finish_task import finish_task
from octoforge_core.tasks._rows import to_task, to_task_row
from octoforge_core.tasks.api import Task, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.models import TaskRow
from octoforge_core.tasks.ports import TaskList
from octoforge_core.tasks.requests import TaskCompletion
from octoforge_core.time import utc_now


class SqlAlchemyTaskStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(self, task: Task) -> None:
        async with write_session(self._session_factory) as session:
            session.add(to_task_row(task))

    async def get(self, task_id: str) -> Task:
        async with read_session(self._session_factory) as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            return to_task(row)

    async def list(self, dialog_id: str) -> list[Task]:
        async with read_session(self._session_factory) as session:
            result = await session.scalars(
                select(TaskRow).where(TaskRow.dialog_id == dialog_id).order_by(TaskRow.created_at)
            )
            return [to_task(row) for row in result.all()]

    async def mark_done(self, task_id: str, result: str, *, delivered: bool = False) -> Task:
        return await self._finish(
            TaskCompletion(task_id, TaskStatus.DONE, result, delivered=delivered)
        )

    async def mark_failed(self, task_id: str, error: str, *, delivered: bool = False) -> Task:
        return await self._finish(
            TaskCompletion(task_id, TaskStatus.FAILED, error=error, delivered=delivered)
        )

    async def mark_cancelled(self, task_id: str) -> Task:
        return await self._finish(TaskCompletion(task_id, TaskStatus.CANCELLED))

    async def delete(self, task_id: str) -> None:
        async with write_session(self._session_factory) as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            await session.delete(row)

    async def delete_for_dialog(self, dialog_id: str) -> int:
        async with write_session(self._session_factory) as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(delete(TaskRow).where(TaskRow.dialog_id == dialog_id)),
            )
            return result.rowcount or 0

    async def list_orphaned(self, dialog_id: str | None = None) -> TaskList:
        orphaned, _ = await self.list_for_recovery(dialog_id)
        return orphaned

    async def list_undelivered(self, dialog_id: str | None = None) -> TaskList:
        _, undelivered = await self.list_for_recovery(dialog_id)
        return undelivered

    async def list_for_recovery(
        self,
        dialog_id: str | None = None,
    ) -> tuple[TaskList, TaskList]:
        query = _recovery_query(dialog_id)
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        async with read_session(self._session_factory) as session:
            tasks = [to_task(row) for row in (await session.scalars(query)).all()]
        return (
            [task for task in tasks if task.status in active],
            [task for task in tasks if task.status not in active],
        )

    async def mark_delivered(self, task_id: str) -> None:
        async with write_session(self._session_factory) as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(TaskRow).where(TaskRow.id == task_id).values(delivered_at=utc_now())
                ),
            )
            if result.rowcount == 0:
                raise TaskNotFoundError(task_id)

    async def _finish(self, completion: TaskCompletion) -> Task:
        return await finish_task(self._session_factory, completion)


def _recovery_query(dialog_id: str | None) -> Select[tuple[TaskRow]]:
    query = (
        select(TaskRow)
        .where(
            or_(
                TaskRow.status.in_((TaskStatus.PENDING.value, TaskStatus.RUNNING.value)),
                sa_and(
                    TaskRow.status.in_((TaskStatus.DONE.value, TaskStatus.FAILED.value)),
                    TaskRow.delivered_at.is_(None),
                ),
            )
        )
        .order_by(TaskRow.created_at)
    )
    return query if dialog_id is None else query.where(TaskRow.dialog_id == dialog_id)
