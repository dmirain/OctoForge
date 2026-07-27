"""Tools of the tasks module: create, list and delete deferred work.

`task_create` covers both immediate background work (no `schedule`) and cron
jobs (with `schedule`); `task_list` shows both in two sections; `task_delete`
stops a task or removes a cron job. The two stores stay separate — only the
tool surface is unified; the schedule path delegates to the cron domain
(`create_job`).
"""

from typing import Any

from octoforge_core.cron.api import (
    DELETED_MESSAGE,
    NO_JOBS_MESSAGE,
    ONE_SHOT_PARAM,
    PROMPT_PARAM,
    SCHEDULE_PARAM,
    TIMEZONE_PARAM,
    TITLE_PARAM,
    CronJobDraft,
    CronJobNotFoundError,
    CronStore,
    create_job,
    format_job,
    prompt_preview,
)
from octoforge_core.tasks.api import Task, TaskKind, TaskNotFoundError, TaskStatus
from octoforge_core.tasks.store import TaskStore
from octoforge_core.tools.base import TaskDeleteOutcome, ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

CREATE_NAME = "task_create"
CREATE_DESCRIPTION = (
    "Create deferred work. Without 'schedule': a background task solved right now — "
    "the result comes once, when it is ready; use it to keep the conversation free "
    "during long work. With 'schedule': a cron job waking you with the prompt — "
    "recurring, or one-time with one_shot=true. Make the prompt self-contained: "
    "this conversation's context may be gone when the task runs."
)
DEFAULT_TIMEZONE = "UTC"
CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": TITLE_PARAM,
        "prompt": PROMPT_PARAM,
        "schedule": SCHEDULE_PARAM,
        "timezone": TIMEZONE_PARAM,
        "one_shot": ONE_SHOT_PARAM,
    },
    "required": ["title", "prompt"],
}

LIST_NAME = "task_list"
LIST_DESCRIPTION = (
    "List your deferred work: background tasks of this dialog (running or awaiting "
    "result delivery — delivered results stay in the conversation) and your "
    "scheduled cron jobs with next fire times. Both include a preview of the "
    "prompt the work runs with."
)
LIST_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
NO_WORK_MESSAGE = "no tasks or scheduled jobs"
NO_TASKS_MESSAGE = "no tasks"
TASKS_SECTION = "background tasks:"
JOBS_SECTION = "scheduled jobs:"
_ACTIVE_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING)
_DELIVERABLE_STATUSES = (TaskStatus.DONE, TaskStatus.FAILED)

DELETE_NAME = "task_delete"
DELETE_DESCRIPTION = (
    "Stop a background task or delete a cron job by id (as reported by task_list). "
    "A running task is stopped and stays in the history as cancelled; a scheduled "
    "job is removed so it never fires again."
)
DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {
            "type": "string",
            "description": "Task or cron job id as reported by task_list",
        },
    },
    "required": ["task_id"],
}
STOPPED_TASK_MESSAGE = "stopped task {task_id}"
NOT_FOUND_MESSAGE = "error: no task or cron job with this id"
SELF_DELETE_MESSAGE = "error: a running task cannot delete itself"
NO_SPAWNER_MESSAGE = "task spawning is not available in this context"


class TaskCreateTool:
    """Creates immediate background tasks (spawner) or cron jobs (schedule)."""

    def __init__(self, cron_store: CronStore) -> None:
        self._cron_store = cron_store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=CREATE_NAME,
            description=CREATE_DESCRIPTION,
            parameters_schema=CREATE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        title = _non_empty_string(arguments.get("title"), "title")
        prompt = _non_empty_string(arguments.get("prompt"), "prompt")
        schedule = arguments.get("schedule")
        if schedule is None:
            if arguments.get("one_shot") is True:
                raise ToolArgumentsError("one_shot requires a schedule")
            if context.task_spawner is None:
                raise ToolArgumentsError(NO_SPAWNER_MESSAGE)
            return await context.task_spawner.spawn(title, prompt)
        schedule = _non_empty_string(schedule, "schedule")
        timezone = arguments.get("timezone")
        if timezone is None:
            timezone = DEFAULT_TIMEZONE
        timezone = _non_empty_string(timezone, "timezone")
        draft = CronJobDraft(
            user_id=context.user_id,
            channel=context.channel,
            title=title,
            schedule=schedule,
            prompt=prompt,
            timezone=timezone,
            one_shot=arguments.get("one_shot") is True,
        )
        return await create_job(self._cron_store, draft)


class TaskListTool:
    """Lists background tasks of the dialog and the user's cron jobs."""

    def __init__(self, store: TaskStore, cron_store: CronStore) -> None:
        self._store = store
        self._cron_store = cron_store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=LIST_NAME,
            description=LIST_DESCRIPTION,
            parameters_schema=LIST_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        all_tasks = await self._store.list(context.dialog_id)
        tasks = [task for task in all_tasks if _is_visible(task)]
        jobs = await self._cron_store.list_for_user(context.user_id)
        if not tasks and not jobs:
            return NO_WORK_MESSAGE
        task_lines = "\n".join(_format_task(task) for task in tasks) or NO_TASKS_MESSAGE
        job_lines = "\n".join(format_job(job) for job in jobs) or NO_JOBS_MESSAGE
        return f"{TASKS_SECTION}\n{task_lines}\n\n{JOBS_SECTION}\n{job_lines}"


class TaskDeleteTool:
    """Stops a background task (the row is kept as cancelled) or deletes a cron job."""

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
        task_id = _non_empty_string(arguments.get("task_id"), "task_id")
        task = await self._find_task(task_id, context.dialog_id)
        if task is not None:
            return await self._stop_task(task, context)
        return await self._delete_cron_job(task_id, context)

    async def _find_task(self, task_id: str, dialog_id: str) -> Task | None:
        """Return the task when it exists and belongs to this dialog."""
        try:
            task = await self._store.get(task_id)
        except TaskNotFoundError:
            return None
        if task.dialog_id != dialog_id:
            return None
        return task

    async def _stop_task(self, task: Task, context: ToolContext) -> str:
        if task.id == context.owner_task_id:
            return SELF_DELETE_MESSAGE
        if task.status in _ACTIVE_STATUSES:
            stopped = False
            if context.task_deleter is not None:
                outcome = await context.task_deleter.delete(task.id)
                stopped = outcome is TaskDeleteOutcome.DELETED
            if not stopped:
                # no live process (orphaned row or no deleter): cancel directly,
                # a stopped process is marked CANCELLED by its finalization instead
                await self._store.mark_cancelled(task)
        return STOPPED_TASK_MESSAGE.format(task_id=task.id)

    async def _delete_cron_job(self, job_id: str, context: ToolContext) -> str:
        try:
            await self._cron_store.delete_for_user(context.user_id, job_id)
        except CronJobNotFoundError:
            return NOT_FOUND_MESSAGE
        return DELETED_MESSAGE.format(job_id=job_id)


def _non_empty_string(raw: object, argument: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{argument} must be a non-empty string")
    return raw


def _is_visible(task: Task) -> bool:
    """Whether the task belongs in `task_list`: active or awaiting delivery.

    Answer-kind tasks are internal mechanics, and rows are kept forever now —
    delivered terminal tasks are history, not work in flight.
    """
    if task.kind is not TaskKind.RUN:
        return False
    if task.status in _ACTIVE_STATUSES:
        return True
    return task.status in _DELIVERABLE_STATUSES and task.delivered_at is None


def _format_task(task: Task) -> str:
    line = f"{task.id} [{task.status.value}] {task.title}"
    prompt = task.input.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        line += f" — prompt: {prompt_preview(prompt)!r}"
    if task.result is not None:
        line += f" -> {task.result}"
    if task.error is not None:
        line += f" -> error: {task.error}"
    return line
