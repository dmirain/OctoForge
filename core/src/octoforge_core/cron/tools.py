"""Pause/resume tools of the cron module.

Job creation, rendering and the shared tool-schema fragments live on the
module boundary (`cron/api.py`): the agent-facing create/list/delete surface
is unified in `tasks/tools.py` (`task_create` with a schedule delegates to
`cron.api.create_job`), and a cross-module import of another module's tools
implementation was exactly the coupling the 2026-07-27 boundary audit
removed. What stays registered from here is pause/resume — the operations
with no task-side counterpart.
"""

from dataclasses import replace
from typing import Any

from octoforge_core.cron.api import (
    JOB_NOT_FOUND_MESSAGE,
    CronEnablement,
    CronJobNotFoundError,
    CronScheduleError,
    CronStore,
    compute_next_fire,
    format_job,
)
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext, ToolSpec

PAUSE_NAME = "cron_pause"
RESUME_NAME = "cron_resume"

JOB_ID_PARAM: dict[str, Any] = {
    "type": "string",
    "description": "Job id as reported by task_create/task_list",
}

JOB_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"job_id": JOB_ID_PARAM},
    "required": ["job_id"],
}


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
        return await _set_enabled(
            self._store,
            CronEnablement(context.user_id, str(arguments["job_id"]), False),
        )


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
        return await _set_enabled(
            self._store,
            CronEnablement(context.user_id, str(arguments["job_id"]), True),
        )


async def _set_enabled(store: CronStore, request: CronEnablement) -> str:
    """Shared pause/resume flow with ownership and schedule checks."""
    try:
        job = await store.get(request.job_id)
    except CronJobNotFoundError:
        return JOB_NOT_FOUND_MESSAGE
    if job.user_id != request.user_id:
        return JOB_NOT_FOUND_MESSAGE
    next_fire_at = None
    if request.enabled:
        try:
            next_fire_at = compute_next_fire(job.schedule, job.timezone, utc_now())
        except CronScheduleError as exc:
            return f"error: {exc}"
    updated = await store.set_enabled(replace(request, next_fire_at=next_fire_at))
    verb = "resumed" if request.enabled else "paused"
    return f"{verb} cron job {updated.id}\n" + format_job(updated)
