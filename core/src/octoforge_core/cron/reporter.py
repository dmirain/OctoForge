"""Cron outcome reporter: folds fired-process outcomes back into the store.

Implements the generic `TaskOutcomeListener` port from `agent/runner.py`: the
actor reports the terminal status of every cron-tagged task, and this adapter
applies the cron policy — record the outcome, delete one-shot reminders after
their single attempt (success or failure), keep the schedule otherwise.
"""

import logging

from octoforge_core.cron.api import CronJobNotFoundError, CronStore
from octoforge_core.tasks.models import Task, TaskStatus

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_CHARS = 500
CRON_JOB_ID_INPUT_KEY = "cron_job_id"


class CronOutcomeReporter:
    """TaskOutcomeListener recording fire outcomes without retries.

    Policy: DONE deletes a one-shot job (and resets the retry streak of a
    recurring one); FAILED is recorded as `last_status`/`last_error` and also
    deletes a one-shot job — one shot means one attempt, the job keeps its
    regular schedule otherwise; CANCELLED is recorded the same way (the user
    cancelled on purpose). No outcome ever reschedules a job (`retry_at` is
    always None).
    """

    def __init__(self, store: CronStore) -> None:
        self._store = store

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
        if job.one_shot and status in (TaskStatus.DONE, TaskStatus.FAILED):
            await self._store.delete_for_user(job.user_id, job.id)
            return
        await self._store.record_fire_result(
            job_id,
            status,
            error=_error_for(task, status),
            retry_at=None,
        )


def _error_for(task: Task, status: TaskStatus) -> str | None:
    if status is TaskStatus.FAILED:
        return (task.error or "unknown error")[:LAST_ERROR_MAX_CHARS]
    if status is TaskStatus.CANCELLED:
        return "cancelled"
    return None
