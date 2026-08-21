"""In-memory TaskStore used in tests and as a behavioral reference."""

from octoforge_core.tasks.api import Task, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.ports import TaskList
from octoforge_core.time import utc_now


class InMemoryTaskStore:
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
        matched = [key for key, task in self._tasks.items() if task.dialog_id == dialog_id]
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

    async def list_for_recovery(
        self,
        dialog_id: str | None = None,
    ) -> tuple[TaskList, TaskList]:
        return await self.list_orphaned(dialog_id), await self.list_undelivered(dialog_id)

    async def mark_delivered(self, task_id: str) -> None:
        task = await self.get(task_id)
        task.delivered_at = utc_now()
