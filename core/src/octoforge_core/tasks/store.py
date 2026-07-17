"""In-memory TaskStore implementation."""

from octoforge_core.tasks.errors import TaskNotFoundError
from octoforge_core.tasks.models import Task, TaskStatus
from octoforge_core.time import utc_now


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

    async def next_pending(self) -> Task | None:
        for task in self._tasks.values():
            if task.status is TaskStatus.PENDING:
                return task
        return None

    async def mark_running(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = utc_now()

    async def mark_done(self, task: Task, result: str) -> None:
        task.status = TaskStatus.DONE
        task.result = result
        task.finished_at = utc_now()

    async def mark_failed(self, task: Task, error: str) -> None:
        task.status = TaskStatus.FAILED
        task.error = error
        task.finished_at = utc_now()

    async def mark_delivered(self, task_id: str) -> None:
        task = await self.get(task_id)
        task.result_delivered = True
