"""TaskStore port and its implementations (in-memory and SQL).

Task rows are kept forever: a terminal task stays in the store and its
`delivered_at` timestamp (set by `mark_delivered`) records that the result
reached the user transport. `list_undelivered` therefore returns terminal
tasks with `delivered_at IS NULL` — crash leftovers and fresh finals alike.
"""

from typing import Any, Protocol, cast

from sqlalchemy import and_ as sa_and
from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
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

    async def delete(self, task_id: str) -> None:
        """Delete the task row or raise TaskNotFoundError."""
        ...

    async def delete_for_dialog(self, dialog_id: str) -> int:
        """Delete every task row of the dialog; return the count (admin dialog deletion)."""
        ...

    async def list_orphaned(self, dialog_id: str | None = None) -> TaskList:
        """PENDING/RUNNING tasks (read-only, no mutation); None: every dialog.

        Their in-memory executor died with whoever was running them — a
        restarted process, or a peer that handed the dialog over. Scoped to a
        dialog when recovery is for that dialog alone: with more than one
        process alive, restarting a peer's live task would run it twice.
        """
        ...

    async def list_undelivered(self, dialog_id: str | None = None) -> TaskList:
        """Terminal (DONE/FAILED) tasks whose result never reached the user."""
        ...

    async def list_for_recovery(self, dialog_id: str | None = None) -> tuple[TaskList, TaskList]:
        """`(orphaned, undelivered)` in one lookup: recovery always wants both."""
        ...

    async def mark_delivered(self, task_id: str) -> None:
        """Stamp the task's result as delivered to the user transport."""
        ...

    async def mark_done(self, task_id: str, result: str, *, delivered: bool = False) -> Task:
        """Finish the task with its result; return the stored row as it now is.

        `delivered` stamps delivery in the same write, for the cases where it
        is already certain: an answer the user watched stream, or a
        deliberately empty one that must never be redelivered. It is NOT for
        a result going through the outbox — that one is stamped after it has
        actually been broadcast, and stamping it early would lose it.
        """
        ...

    async def mark_failed(self, task_id: str, error: str, *, delivered: bool = False) -> Task:
        """Fail the task with its error; return the stored row as it now is."""
        ...

    async def mark_cancelled(self, task_id: str) -> Task:
        """Cancel the task; return the stored row as it now is."""
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

    async def mark_done(self, task_id: str, result: str, *, delivered: bool = False) -> Task:
        task = await self.get(task_id)
        task.status = TaskStatus.DONE
        task.result = result
        task.finished_at = utc_now()
        if delivered:
            task.delivered_at = task.finished_at
        return task

    async def mark_failed(self, task_id: str, error: str, *, delivered: bool = False) -> Task:
        task = await self.get(task_id)
        task.status = TaskStatus.FAILED
        task.error = error
        task.finished_at = utc_now()
        if delivered:
            task.delivered_at = task.finished_at
        return task

    async def mark_cancelled(self, task_id: str) -> Task:
        task = await self.get(task_id)
        task.status = TaskStatus.CANCELLED
        task.finished_at = utc_now()
        return task

    async def delete(self, task_id: str) -> None:
        task = await self.get(task_id)
        del self._tasks[task.id]

    async def delete_for_dialog(self, dialog_id: str) -> int:
        matched = [task_id for task_id, task in self._tasks.items() if task.dialog_id == dialog_id]
        for task_id in matched:
            del self._tasks[task_id]
        return len(matched)

    async def list_orphaned(self, dialog_id: str | None = None) -> TaskList:
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        return [
            task
            for task in self._tasks.values()
            if task.status in active and (dialog_id is None or task.dialog_id == dialog_id)
        ]

    async def list_undelivered(self, dialog_id: str | None = None) -> TaskList:
        finished = (TaskStatus.DONE, TaskStatus.FAILED)
        return [
            task
            for task in self._tasks.values()
            if task.status in finished
            and task.delivered_at is None
            and (dialog_id is None or task.dialog_id == dialog_id)
        ]

    async def list_for_recovery(self, dialog_id: str | None = None) -> tuple[TaskList, TaskList]:
        return (
            await self.list_orphaned(dialog_id),
            await self.list_undelivered(dialog_id),
        )

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

    async def mark_done(self, task_id: str, result: str, *, delivered: bool = False) -> Task:
        """Finish the task, optionally stamping delivery in the same write."""
        return await self._finish(
            task_id, status=TaskStatus.DONE, result=result, delivered=delivered
        )

    async def mark_failed(self, task_id: str, error: str, *, delivered: bool = False) -> Task:
        """Fail the task, optionally stamping delivery in the same write."""
        return await self._finish(
            task_id, status=TaskStatus.FAILED, error=error, delivered=delivered
        )

    async def mark_cancelled(self, task_id: str) -> Task:
        """Cancel the task."""
        return await self._finish(task_id, status=TaskStatus.CANCELLED)

    async def delete(self, task_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFoundError(task_id)
            await session.delete(row)
            await session.commit()

    async def delete_for_dialog(self, dialog_id: str) -> int:
        async with self._session_factory() as session:
            # DML executes into a CursorResult at runtime; narrow for rowcount.
            result = cast(
                "CursorResult[Any]",
                await session.execute(delete(TaskRow).where(TaskRow.dialog_id == dialog_id)),
            )
            await session.commit()
            return result.rowcount or 0

    async def list_orphaned(self, dialog_id: str | None = None) -> TaskList:
        orphaned, _ = await self.list_for_recovery(dialog_id)
        return orphaned

    async def list_undelivered(self, dialog_id: str | None = None) -> TaskList:
        _, undelivered = await self.list_for_recovery(dialog_id)
        return undelivered

    async def list_for_recovery(self, dialog_id: str | None = None) -> tuple[TaskList, TaskList]:
        """Both recovery sets in one query: (orphaned, undelivered).

        Recovery always wants both, they live in the same table under the same
        dialog, and their conditions are disjoint — so asking twice buys
        nothing and costs a round trip. Split here rather than in SQL because
        the caller does different things with each.
        """
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
        if dialog_id is not None:
            query = query.where(TaskRow.dialog_id == dialog_id)
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        async with self._session_factory() as session:
            tasks = [_to_task(row) for row in (await session.scalars(query)).all()]
        return (
            [task for task in tasks if task.status in active],
            [task for task in tasks if task.status not in active],
        )

    async def mark_delivered(self, task_id: str) -> None:
        """Stamp delivery. One UPDATE: `rowcount` says whether the task exists."""
        async with self._session_factory() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(TaskRow).where(TaskRow.id == task_id).values(delivered_at=utc_now())
                ),
            )
            if result.rowcount == 0:
                raise TaskNotFoundError(task_id)
            await session.commit()

    async def _finish(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
        delivered: bool = False,
    ) -> Task:
        """Write the terminal state and read the row back in one statement.

        `RETURNING` is what removes two round trips: the caller used to fetch
        the task before writing it (nothing was decided on what it read) and
        the delivery path used to fetch it again afterwards. Both get the row
        from the write itself now.
        """
        now = utc_now()
        values: dict[str, Any] = {"status": status.value, "finished_at": now}
        if result is not None:
            values["result"] = result
        if error is not None:
            values["error"] = error
        if delivered:
            values["delivered_at"] = now
        async with self._session_factory() as session:
            row = (
                await session.scalars(
                    update(TaskRow).where(TaskRow.id == task_id).values(**values).returning(TaskRow)
                )
            ).first()
            if row is None:
                raise TaskNotFoundError(task_id)
            task = _to_task(row)
            await session.commit()
        return task


def _to_task_row(task: Task) -> TaskRow:
    return TaskRow(
        id=task.id,
        dialog_id=task.dialog_id,
        user_id=task.user_id,
        channel=task.channel,
        exchange_id=task.exchange_id,
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
        exchange_id=row.exchange_id,
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
