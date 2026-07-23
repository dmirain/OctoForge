"""Cron job helpers and the pause/resume tools over the CronStore port.

Job creation and rendering stay in the cron domain (`create_job`, `format_job`)
while the agent-facing create/list/delete tools are unified in the task tools
(`tasks/tools.py`): the schedule path of `task_create` delegates here. What
remains registered from this module is pause/resume — the operations with no
task-side counterpart.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from octoforge_core.cron.api import (
    CronJob,
    CronJobNotFoundError,
    CronScheduleError,
    CronStore,
    compute_next_fire,
)
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext, ToolSpec

PAUSE_NAME = "cron_pause"
RESUME_NAME = "cron_resume"

NO_JOBS_MESSAGE = "no cron jobs"
JOB_NOT_FOUND_MESSAGE = "error: cron job not found"
DELETED_MESSAGE = "deleted cron job {job_id}"
DUPLICATE_MESSAGE = "already exists: cron job {job_id}"

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
JOB_ID_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Job id as reported by task_create/task_list",
}

JOB_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"job_id": JOB_ID_PARAM},
    "required": ["job_id"],
}


@dataclass(frozen=True, slots=True)
class CronJobDraft:
    """Fields of a cron job to create; id and timestamps are assigned at creation."""

    user_id: str
    channel: str
    title: str
    schedule: str
    prompt: str
    timezone: str
    one_shot: bool


async def create_job(store: CronStore, draft: CronJobDraft) -> str:
    """Create a cron job owned by the user; returns the confirmation text.

    Identical jobs are deduplicated: creating the same job twice returns the
    existing one instead of a second record.
    """
    try:
        next_fire_at = compute_next_fire(draft.schedule, draft.timezone, utc_now())
    except CronScheduleError as exc:
        return f"error: {exc}"
    duplicate = await _find_duplicate(store, draft)
    if duplicate is not None:
        return DUPLICATE_MESSAGE.format(job_id=duplicate.id) + "\n" + format_job(duplicate)
    job = CronJob(
        id=uuid.uuid4().hex,
        user_id=draft.user_id,
        channel=draft.channel,
        title=draft.title,
        schedule=draft.schedule,
        timezone=draft.timezone,
        prompt=draft.prompt,
        enabled=True,
        next_fire_at=next_fire_at,
        last_fire_at=None,
        claimed_by=None,
        claimed_at=None,
        created_at=utc_now(),
        one_shot=draft.one_shot,
        last_status=None,
        last_error=None,
        retry_count=0,
    )
    stored = await store.create(job)
    return f"created cron job {stored.id}\n" + format_job(stored)


async def _find_duplicate(store: CronStore, draft: CronJobDraft) -> CronJob | None:
    """Return the identical existing job, if any (idempotence guard)."""
    for job in await store.list_for_user(draft.user_id):
        if (
            job.title == draft.title
            and job.schedule == draft.schedule
            and job.prompt == draft.prompt
            and job.one_shot == draft.one_shot
        ):
            return job
    return None


class CronPauseTool:
    """Pauses one of the calling user's cron jobs."""

    def __init__(self, store: CronStore) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=PAUSE_NAME,
            description="Pause one of your cron jobs: it stays listed but never fires.",
            parameters_schema=JOB_ID_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return await _set_enabled(self._store, context, str(arguments["job_id"]), enabled=False)


class CronResumeTool:
    """Resumes one of the calling user's cron jobs."""

    def __init__(self, store: CronStore) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=RESUME_NAME,
            description="Resume a paused cron job; the next fire time is recomputed from now.",
            parameters_schema=JOB_ID_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return await _set_enabled(self._store, context, str(arguments["job_id"]), enabled=True)


async def _set_enabled(store: CronStore, context: ToolContext, job_id: str, enabled: bool) -> str:
    """Shared pause/resume flow with ownership and schedule checks."""
    try:
        job = await store.get(job_id)
    except CronJobNotFoundError:
        return JOB_NOT_FOUND_MESSAGE
    if job.user_id != context.user_id:
        return JOB_NOT_FOUND_MESSAGE
    next_fire_at = None
    if enabled:
        try:
            next_fire_at = compute_next_fire(job.schedule, job.timezone, utc_now())
        except CronScheduleError as exc:
            return f"error: {exc}"
    updated = await store.set_enabled(context.user_id, job_id, enabled, next_fire_at)
    verb = "resumed" if enabled else "paused"
    return f"{verb} cron job {updated.id}\n" + format_job(updated)


def format_job(job: CronJob) -> str:
    state = "enabled" if job.enabled else "paused"
    line = (
        f"{job.id} [{state}] {job.title!r} — {job.schedule} ({job.timezone}), "
        f"next fire at {job.next_fire_at.isoformat()}"
    )
    if job.one_shot:
        line += ", one-shot"
    if job.last_fire_at is not None:
        line += f", last fire at {job.last_fire_at.isoformat()}"
    if job.last_status is not None:
        line += f", last run: {job.last_status.value}"
        if job.last_error:
            line += f" ({job.last_error})"
    if job.retry_count > 0:
        line += f", retry #{job.retry_count}"
    return line
