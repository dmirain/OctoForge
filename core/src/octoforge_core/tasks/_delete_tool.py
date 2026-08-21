"""The task_delete tool: cancel background work or remove a cron job."""

from typing import Any

from octoforge_core.cron.api import DELETED_MESSAGE, CronJobNotFoundError, CronStore
from octoforge_core.tasks._list_tool import ACTIVE_STATUSES
from octoforge_core.tasks._tool_args import non_empty_string
from octoforge_core.tasks.api import Task, TaskNotFoundError
from octoforge_core.tasks.store import TaskStore
from octoforge_core.tasks.tool_contract import (
    DELETE_DESCRIPTION,
    DELETE_NAME,
    DELETE_SCHEMA,
    NOT_FOUND_MESSAGE,
    SELF_DELETE_MESSAGE,
    STOPPED_TASK_MESSAGE,
)
from octoforge_core.tools.base import TaskDeleteOutcome, ToolContext, ToolSpec


class TaskDeleteTool:
    """Stop a background task or delete a cron job by id."""

    def __init__(self, store: TaskStore, cron_store: CronStore) -> None:
        self._store = store
        self._cron_store = cron_store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=DELETE_NAME,
            description=DELETE_DESCRIPTION,
            parameters_schema=DELETE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        task_id = non_empty_string(arguments.get("task_id"), "task_id")
        task = await self._find_task(task_id, context.dialog_id)
        if task is not None:
            return await self._stop_task(task, context)
        return await self._delete_cron_job(task_id, context)

    async def _find_task(self, task_id: str, dialog_id: str) -> Task | None:
        try:
            task = await self._store.get(task_id)
        except TaskNotFoundError:
            return None
        return task if task.dialog_id == dialog_id else None

    async def _stop_task(self, task: Task, context: ToolContext) -> str:
        if task.id == context.owner_task_id:
            return SELF_DELETE_MESSAGE
        if task.status in ACTIVE_STATUSES:
            stopped = False
            if context.task_deleter is not None:
                outcome = await context.task_deleter.delete(task.id)
                stopped = outcome is TaskDeleteOutcome.DELETED
            if not stopped:
                await self._store.mark_cancelled(task.id)
        return STOPPED_TASK_MESSAGE.format(task_id=task.id)

    async def _delete_cron_job(self, job_id: str, context: ToolContext) -> str:
        try:
            await self._cron_store.delete_for_user(context.user_id, job_id)
        except CronJobNotFoundError:
            return NOT_FOUND_MESSAGE
        return DELETED_MESSAGE.format(job_id=job_id)
