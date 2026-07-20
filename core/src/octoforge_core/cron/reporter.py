"""Cron outcome reporter: folds fired-process outcomes back into the store.

Implements the generic `TaskOutcomeListener` port from `agent/runner.py`: the
actor reports the terminal status of every cron-tagged task, and this adapter
applies the cron policy — retry with exponential backoff on failure, delete
one-shot reminders after the first success, keep the schedule otherwise.
"""

import logging
from datetime import datetime, timedelta

from octoforge_core.cron.api import CronJob, CronJobNotFoundError, CronStore
from octoforge_core.tasks.models import Task, TaskStatus
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_CHARS = 500
CRON_JOB_ID_INPUT_KEY = "cron_job_id"


class CronOutcomeReporter:
    """TaskOutcomeListener recording fire outcomes and scheduling retries.

    Policy: DONE resets the retry streak (and deletes a one-shot job); FAILED
    within the retry budget reschedules the job to `now + base * 2**streak`
    (the streak grows per retry); FAILED exhausted records the failure and the
    job continues on its regular schedule with a fresh budget; CANCELLED is
    recorded without a retry (the user cancelled on purpose).
    """

    def __init__(
        self,
        store: CronStore,
        retry_limit: int,
        backoff_base_seconds: float,
    ) -> None:
        self._store = store
        self._retry_limit = retry_limit
        self._backoff_base_seconds = backoff_base_seconds

    async def report_outcome(self, task: Task, status: TaskStatus) -> None:
        """Record the outcome of a cron-fired task; ignore foreign tasks."""
        job_id = task.input.get(CRON_JOB_ID_INPUT_KEY)
        if not isinstance(job_id, str):
            return
        try:
            job = await self._store.get(job_id)
        except CronJobNotFoundError:
            logger.debug("cron outcome for a deleted job: job=%s task=%s", job_id, task.id)
            return
        if status is TaskStatus.DONE and job.one_shot:
            await self._store.delete_for_user(job.user_id, job.id)
            return
        await self._store.record_fire_result(
            job_id,
            status,
            error=_error_for(task, status),
            retry_at=self._retry_at(job, status),
        )

    def _retry_at(self, job: CronJob, status: TaskStatus) -> datetime | None:
        if status is not TaskStatus.FAILED or job.retry_count >= self._retry_limit:
            return None
        backoff = self._backoff_base_seconds * 2**job.retry_count
        return utc_now() + timedelta(seconds=backoff)


def _error_for(task: Task, status: TaskStatus) -> str | None:
    if status is TaskStatus.FAILED:
        return (task.error or "unknown error")[:LAST_ERROR_MAX_CHARS]
    if status is TaskStatus.CANCELLED:
        return "cancelled"
    return None
