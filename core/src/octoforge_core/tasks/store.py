"""TaskStore port and its implementations (in-memory and SQL).

Task rows are kept forever: a terminal task stays in the store and its
`delivered_at` timestamp (set by `mark_delivered`) records that the result
reached the user transport. `list_undelivered` therefore returns terminal
tasks with `delivered_at IS NULL` — crash leftovers and fresh finals alike.
"""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.tasks.api import Task, TaskKind, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.models import TaskRow
from octoforge_core.time import utc_now

# Inside stores the method named `list` shadows the builtin in class-scope
# annotations, so list-returning signatures alias it here.
TaskList = list[Task]


class TaskStore(Protocol):
    """Persistence port for background tasks."""

    async def add(self, task: Task) -> None:
        """Store a new task."""
        ...

    async def get(self, task_id: str) -> Task:
        """Return the task by id or raise TaskNotFoundError."""
        ...

    async def list(self, dialog_id: str) -> list[Task]:
        """Return tasks of one dialog."""
        ...

    async def mark_done(self, task: Task, result: str) -> None:
        """Mark the task as done with a result."""
        ...

    async def mark_failed(self, task: Task, error: str) -> None:
        """Mark the task as failed with an error."""
        ...

    async def mark_cancelled(self, task: Task) -> None:
        """Mark the task as cancelled; the row is kept, never delivered."""
        ...

    async def delete(self, task_id: str) -> None:
        """Delete the task row or raise TaskNotFoundError."""
        ...

    async def list_orphaned(self) -> TaskList:
        """Return every PENDING/RUNNING task (read-only, no mutation).

        Their in-memory executors (actor pump processes) died with the
        previous service instance, so they can never finish on their own.
        """
        ...

    async def list_undelivered(self) -> TaskList:
        """Return terminal (DONE/FAILED) tasks whose result was not delivered."""
        ...

    async def mark_delivered(self, task_id: str) -> None:
        """Stamp the task's result as delivered to the user transport."""
        ...


class InMemoryTaskStore:
    """Dict-backed task store; used in tests and as a behavioral reference."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    async def add(self, task: Task) -> None:
        self._tasks[task.id] = task

    async def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(task_id) from exc

    async def list(self, dialog_id: str) -> list[Task]:
        return [task for task in self._tasks.values() if task.dialog_id == dialog_id]

    async def mark_done(self, task: Task, result: str) -> None:
        task.status = TaskStatus.DONE
        task.result = result
        task.finished_at = utc_now()

    async def mark_failed(self, task: Task, error: str) -> None:
        task.status = TaskStatus.FAILED
        task.error = error
        task.finished_at = utc_now()

    async def mark_cancelled(self, task: Task) -> None:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()

    async def delete(self, task_id: str) -> None:
        task = await self.get(task_id)
        del self._tasks[task.id]

    async def list_orphaned(self) -> TaskList:
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        return [task for task in self._tasks.values() if task.status in active]

    async def list_undelivered(self) -> TaskList:
        finished = (TaskStatus.DONE, TaskStatus.FAILED)
        return [
            task
            for task in self._tasks.values()
            if task.status in finished and task.delivered_at is None
        ]

    async def mark_delivered(self, task_id: str) -> None:
        task = await self.get(task_id)
        task.delivered_at = utc_now()


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

    async def mark_cancelled(self, task: Task) -> None:
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()
        await self._update(task)

    async def delete(self, task_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            await session.delete(row)
            await session.commit()

    async def list_orphaned(self) -> TaskList:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(TaskRow)
                .where(TaskRow.status.in_((TaskStatus.PENDING.value, TaskStatus.RUNNING.value)))
                .order_by(TaskRow.created_at)
            )
            return [_to_task(row) for row in result.all()]

    async def list_undelivered(self) -> TaskList:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(TaskRow)
                .where(
                    TaskRow.status.in_((TaskStatus.DONE.value, TaskStatus.FAILED.value)),
                    TaskRow.delivered_at.is_(None),
                )
                .order_by(TaskRow.created_at)
            )
            return [_to_task(row) for row in result.all()]

    async def mark_delivered(self, task_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            row.delivered_at = utc_now()
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
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        delivered_at=task.delivered_at,
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
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        delivered_at=row.delivered_at,
    )
