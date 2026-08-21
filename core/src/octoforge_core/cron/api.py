"""Public boundary of the cron module."""

from octoforge_core.cron.jobs import (
    create_job,
    format_job,
    job_quota_refusal,
    prompt_preview,
)
from octoforge_core.cron.ports import CronStore, CronWaker, Scheduler
from octoforge_core.cron.schedule import compute_next_fire, count_missed
from octoforge_core.cron.tool_contract import (
    DELETED_MESSAGE,
    DUPLICATE_MESSAGE,
    JOB_NOT_FOUND_MESSAGE,
    NO_JOBS_MESSAGE,
    ONE_SHOT_PARAM,
    PROMPT_PARAM,
    SCHEDULE_PARAM,
    TIMEZONE_PARAM,
    TITLE_PARAM,
)
from octoforge_core.cron.types import (
    CronClaim,
    CronEnablement,
    CronFireResult,
    CronJob,
    CronJobDraft,
    CronJobNotFoundError,
    CronScheduleError,
    CronWake,
    MissedRuns,
    WakeOutcome,
)

__all__ = [
    "DELETED_MESSAGE",
    "DUPLICATE_MESSAGE",
    "JOB_NOT_FOUND_MESSAGE",
    "NO_JOBS_MESSAGE",
    "ONE_SHOT_PARAM",
    "PROMPT_PARAM",
    "SCHEDULE_PARAM",
    "TIMEZONE_PARAM",
    "TITLE_PARAM",
    "CronClaim",
    "CronEnablement",
    "CronFireResult",
    "CronJob",
    "CronJobDraft",
    "CronJobNotFoundError",
    "CronScheduleError",
    "CronStore",
    "CronWake",
    "CronWaker",
    "MissedRuns",
    "Scheduler",
    "WakeOutcome",
    "compute_next_fire",
    "count_missed",
    "create_job",
    "format_job",
    "job_quota_refusal",
    "prompt_preview",
]
