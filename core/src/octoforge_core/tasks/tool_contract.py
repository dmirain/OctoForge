"""Model-facing names, descriptions, schemas and responses for task tools."""

from typing import Any

from octoforge_core.cron.api import (
    ONE_SHOT_PARAM,
    PROMPT_PARAM,
    SCHEDULE_PARAM,
    TIMEZONE_PARAM,
    TITLE_PARAM,
)

CREATE_NAME = "task_create"
CREATE_DESCRIPTION = (
    "Create deferred work. Without 'schedule': a background task solved right now - "
    "the result comes once, when it is ready; use it to keep the conversation free "
    "during long work. With 'schedule': a cron job waking you with the prompt - "
    "recurring, or one-time with one_shot=true. Make the prompt self-contained."
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
    "List your deferred work: background tasks of this dialog and scheduled cron "
    "jobs with next fire times. Both include a preview of the prompt."
)
LIST_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
NO_WORK_MESSAGE = "no tasks or scheduled jobs"
NO_TASKS_MESSAGE = "no tasks"
TASKS_SECTION = "background tasks:"
JOBS_SECTION = "scheduled jobs:"

DELETE_NAME = "task_delete"
DELETE_DESCRIPTION = (
    "Stop a background task or delete a cron job by id. A running task is stopped "
    "and stays in history as cancelled; a scheduled job is removed."
)
DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "Task or cron job id"},
    },
    "required": ["task_id"],
}
STOPPED_TASK_MESSAGE = "stopped task {task_id}"
NOT_FOUND_MESSAGE = "error: no task or cron job with this id"
SELF_DELETE_MESSAGE = "error: a running task cannot delete itself"
NO_SPAWNER_MESSAGE = "task spawning is not available in this context"
