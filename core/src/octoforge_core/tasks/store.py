"""TaskStore port and its in-memory implementation.

A task row lives only while the task is in flight: terminal rows are deleted
once the outcome is reported (a stopped task) or the result is delivered
(done/failed), so `list_undelivered` normally returns crash leftovers only.
"""

from typing import Protocol

from octoforge_core.tasks.errors import TaskNotFoundError
from octoforge_core.tasks.models import Task, TaskStatus
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

    async def delete(self, task_id: str) -> None:
        """Delete the task row or raise TaskNotFoundError."""
        ...

    async def fail_orphaned(self, error: str) -> TaskList:
        """Fail every PENDING/RUNNING task with `error`; return the affected tasks.

        Their in-memory executors (actor pump processes) died with the previous
        service instance, so they can never finish on their own.
        """
        ...

    async def list_undelivered(self) -> TaskList:
        """Return finished (DONE/FAILED) tasks still having a row (crash leftovers)."""
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

    async def delete(self, task_id: str) -> None:
        task = await self.get(task_id)
        del self._tasks[task.id]

    async def fail_orphaned(self, error: str) -> TaskList:
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        orphaned = [task for task in self._tasks.values() if task.status in active]
        for task in orphaned:
            task.status = TaskStatus.FAILED
            task.error = error
            task.finished_at = utc_now()
        return orphaned

    async def list_undelivered(self) -> TaskList:
        finished = (TaskStatus.DONE, TaskStatus.FAILED)
        return [task for task in self._tasks.values() if task.status in finished]
