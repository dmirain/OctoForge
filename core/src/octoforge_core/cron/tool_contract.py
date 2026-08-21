"""Shared model-facing cron tool parameters and response text."""

from typing import Any

NO_JOBS_MESSAGE = "no cron jobs"
JOB_NOT_FOUND_MESSAGE = "error: cron job not found"
DELETED_MESSAGE = "deleted cron job {job_id}"
DUPLICATE_MESSAGE = "already exists: cron job {job_id}"
QUOTA_MESSAGE = (
    "the plan allows at most {limit} scheduled jobs; delete one first (task_list / task_delete)"
)
PROMPT_PREVIEW_CHARS = 200

TITLE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Short job name, e.g. 'morning report'",
}
SCHEDULE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": (
        "Cron expression, e.g. '0 9 * * *' for daily at 09:00; for a one-shot reminder "
        "include the date fields, e.g. '30 15 21 7 *' for July 21 at 15:30"
    ),
}
PROMPT_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Instruction the agent receives on every firing",
}
TIMEZONE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": 'IANA timezone, e.g. "Europe/Moscow"; use "UTC" if unknown',
}
ONE_SHOT_PARAM: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "true for a one-time reminder: the job fires once and is deleted after "
        "the first successful run; default false (recurring)"
    ),
}
