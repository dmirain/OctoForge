"""Basic skills managing the caller's cron jobs directly, without the HTTP API.

These replace the seeded HTTP tool records (`cron_create_job` etc.) that made
the agent call our own loopback API through `external_call`: the skills work
identically on every surface, including the standalone Telegram runner where
no HTTP listener exists.
"""

import uuid
from typing import Any

from octoforge_core.cron.api import (
    CronJob,
    CronJobNotFoundError,
    CronScheduleError,
    CronStore,
    compute_next_fire,
)
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.time import utc_now

CREATE_NAME = "cron_create"
LIST_NAME = "cron_list"
DELETE_NAME = "cron_delete"
PAUSE_NAME = "cron_pause"
RESUME_NAME = "cron_resume"

NO_JOBS_MESSAGE = "no cron jobs"
JOB_NOT_FOUND_MESSAGE = "error: cron job not found"
DELETED_MESSAGE = "deleted cron job {job_id}"

TITLE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Short job name, e.g. 'morning report'",
}
SCHEDULE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Cron expression, e.g. '0 9 * * *' for daily at 09:00",
}
PROMPT_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Instruction the agent receives on every firing",
}
TIMEZONE_PARAM: dict[str, Any] = {
    "type": "string",
    "description": 'IANA timezone, e.g. "Europe/Moscow"; use "UTC" if unknown',
}
JOB_ID_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Job id as reported by cron_create/cron_list",
}

CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": TITLE_PARAM,
        "schedule": SCHEDULE_PARAM,
        "prompt": PROMPT_PARAM,
        "timezone": TIMEZONE_PARAM,
    },
    "required": ["title", "schedule", "prompt", "timezone"],
}
EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
JOB_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"job_id": JOB_ID_PARAM},
    "required": ["job_id"],
}


class CronCreateSkill:
    """Creates a cron job owned by the calling user."""

    def __init__(self, store: CronStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=CREATE_NAME,
            description="Create a recurring cron job that wakes the agent with a prompt.",
            parameters_schema=CREATE_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        title = str(arguments["title"])
        schedule = str(arguments["schedule"])
        prompt = str(arguments["prompt"])
        timezone = str(arguments["timezone"])
        try:
            next_fire_at = compute_next_fire(schedule, timezone, utc_now())
        except CronScheduleError as exc:
            return f"error: {exc}"
        job = CronJob(
            id=uuid.uuid4().hex,
            user_id=context.user_id,
            channel=context.channel,
            title=title,
            schedule=schedule,
            timezone=timezone,
            prompt=prompt,
            enabled=True,
            next_fire_at=next_fire_at,
            last_fire_at=None,
            claimed_by=None,
            claimed_at=None,
            created_at=utc_now(),
        )
        stored = await self._store.create(job)
        return f"created cron job {stored.id}\n" + _format_job(stored)


class CronListSkill:
    """Lists the calling user's cron jobs."""

    def __init__(self, store: CronStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=LIST_NAME,
            description="List your cron jobs with schedules, state and next fire time.",
            parameters_schema=EMPTY_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        jobs = await self._store.list_for_user(context.user_id)
        if not jobs:
            return NO_JOBS_MESSAGE
        return "\n".join(_format_job(job) for job in jobs)


class CronDeleteSkill:
    """Deletes one of the calling user's cron jobs."""

    def __init__(self, store: CronStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=DELETE_NAME,
            description="Delete one of your cron jobs by id.",
            parameters_schema=JOB_ID_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        job_id = str(arguments["job_id"])
        try:
            await self._store.delete_for_user(context.user_id, job_id)
        except CronJobNotFoundError:
            return JOB_NOT_FOUND_MESSAGE
        return DELETED_MESSAGE.format(job_id=job_id)


class CronPauseSkill:
    """Pauses one of the calling user's cron jobs."""

    def __init__(self, store: CronStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=PAUSE_NAME,
            description="Pause one of your cron jobs: it stays listed but never fires.",
            parameters_schema=JOB_ID_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        return await _set_enabled(self._store, context, str(arguments["job_id"]), enabled=False)


class CronResumeSkill:
    """Resumes one of the calling user's cron jobs."""

    def __init__(self, store: CronStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=RESUME_NAME,
            description="Resume a paused cron job; the next fire time is recomputed from now.",
            parameters_schema=JOB_ID_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        return await _set_enabled(self._store, context, str(arguments["job_id"]), enabled=True)


async def _set_enabled(store: CronStore, context: SkillContext, job_id: str, enabled: bool) -> str:
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
    return f"{verb} cron job {updated.id}\n" + _format_job(updated)


def _format_job(job: CronJob) -> str:
    state = "enabled" if job.enabled else "paused"
    line = (
        f"{job.id} [{state}] {job.title!r} — {job.schedule} ({job.timezone}), "
        f"next fire at {job.next_fire_at.isoformat()}"
    )
    if job.last_fire_at is not None:
        line += f", last fire at {job.last_fire_at.isoformat()}"
    return line
