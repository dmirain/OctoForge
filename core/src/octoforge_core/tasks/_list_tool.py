"""The task_list tool and human-readable task rendering."""

from typing import Any

from octoforge_core.cron.api import NO_JOBS_MESSAGE, CronStore, format_job, prompt_preview
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.tasks.store import TaskStore
from octoforge_core.tasks.tool_contract import (
    JOBS_SECTION,
    LIST_DESCRIPTION,
    LIST_NAME,
    LIST_SCHEMA,
    NO_TASKS_MESSAGE,
    NO_WORK_MESSAGE,
    TASKS_SECTION,
)
from octoforge_core.tools.base import ToolContext, ToolSpec

ACTIVE_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING)
DELIVERABLE_STATUSES = (TaskStatus.DONE, TaskStatus.FAILED)


class TaskListTool:
    """List background tasks of this dialog and the user's cron jobs."""

    def __init__(self, store: TaskStore, cron_store: CronStore) -> None:
        self._store = store
        self._cron_store = cron_store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=LIST_NAME, description=LIST_DESCRIPTION, parameters_schema=LIST_SCHEMA)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        tasks = [task for task in await self._store.list(context.dialog_id) if is_visible(task)]
        jobs = await self._cron_store.list_for_user(context.user_id)
        if not tasks and not jobs:
            return NO_WORK_MESSAGE
        task_lines = "\n".join(format_task(task) for task in tasks) or NO_TASKS_MESSAGE
        job_lines = "\n".join(format_job(job) for job in jobs) or NO_JOBS_MESSAGE
        return f"{TASKS_SECTION}\n{task_lines}\n\n{JOBS_SECTION}\n{job_lines}"


def is_visible(task: Task) -> bool:
    if task.kind is not TaskKind.RUN:
        return False
    if task.status in ACTIVE_STATUSES:
        return True
    return task.status in DELIVERABLE_STATUSES and task.delivered_at is None


def format_task(task: Task) -> str:
    line = f"{task.id} [{task.status.value}] {task.title}"
    prompt = task.input.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        line += f" - prompt: {prompt_preview(prompt)!r}"
    if task.result is not None:
        line += f" -> {task.result}"
    if task.error is not None:
        line += f" -> error: {task.error}"
    return line
