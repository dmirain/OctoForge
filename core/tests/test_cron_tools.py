"""Tests for the cron pause/resume tools (creation/list/delete live in the task tools)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from octoforge_core.cron.api import (
    CronClaim,
    CronEnablement,
    CronFireResult,
    CronJob,
    CronJobNotFoundError,
)
from octoforge_core.cron.tools import CronPauseTool, CronResumeTool
from octoforge_core.time import utc_now
from octoforge_core.tools.base import ToolContext

USER_ID = "alice"
OTHER_USER_ID = "bob"
CHANNEL = "telegram"
DIALOG_ID = "dialog-1"
JOB_ID = "job-1"

VALID_SCHEDULE = "0 9 * * *"
VALID_TIMEZONE = "Europe/Moscow"
CONTEXT = ToolContext(user_id=USER_ID, channel=CHANNEL, dialog_id=DIALOG_ID)


class FakeCronStore:
    """In-memory CronStore; lease methods are irrelevant to the tools."""

    def __init__(self) -> None:
        self.jobs: dict[str, CronJob] = {}

    async def create(self, job: CronJob) -> CronJob:
        self.jobs[job.id] = job
        return job

    async def get(self, job_id: str) -> CronJob:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise CronJobNotFoundError(job_id) from exc

    async def list_for_user(self, user_id: str) -> list[CronJob]:
        return [job for job in self.jobs.values() if job.user_id == user_id]

    async def delete_for_user(self, user_id: str, job_id: str) -> None:
        job = await self.get(job_id)
        if job.user_id != user_id:
            raise CronJobNotFoundError(job_id)
        del self.jobs[job_id]

    async def set_enabled(self, request: CronEnablement) -> CronJob:
        job = await self._get_owned(request.user_id, request.job_id)
        updated = replace(
            job,
            enabled=request.enabled,
            next_fire_at=(
                request.next_fire_at if request.next_fire_at is not None else job.next_fire_at
            ),
        )
        self.jobs[request.job_id] = updated
        return updated

    async def _get_owned(self, user_id: str, job_id: str) -> CronJob:
        job = await self.get(job_id)
        if job.user_id != user_id:
            raise CronJobNotFoundError(job_id)
        return job

    async def list_due(self, now: datetime, stale_before: datetime, limit: int) -> list[CronJob]:
        raise NotImplementedError

    async def claim(self, request: CronClaim) -> bool:
        raise NotImplementedError

    async def release_claim(self, job_id: str) -> None:
        raise NotImplementedError

    async def complete_fire(self, job_id: str, fired_at: datetime, next_fire_at: datetime) -> None:
        raise NotImplementedError

    async def record_fire_result(self, result: CronFireResult) -> None:
        raise NotImplementedError


def make_job(**overrides: object) -> CronJob:
    """A valid job of USER_ID; fields overridable per test."""
    base = CronJob(
        id=JOB_ID,
        user_id=USER_ID,
        channel=CHANNEL,
        title="morning report",
        schedule=VALID_SCHEDULE,
        timezone=VALID_TIMEZONE,
        prompt="good morning",
        enabled=True,
        next_fire_at=utc_now() + timedelta(hours=1),
        last_fire_at=None,
        claimed_by=None,
        claimed_at=None,
        created_at=utc_now(),
        one_shot=False,
        last_status=None,
        last_error=None,
        retry_count=0,
    )
    return replace(base, **overrides)


async def test_pause_disables_the_job() -> None:
    store = FakeCronStore()
    await store.create(make_job())

    result = await CronPauseTool(store).execute({"job_id": JOB_ID}, CONTEXT)

    assert result.startswith(f"paused cron job {JOB_ID}")
    assert not store.jobs[JOB_ID].enabled


async def test_resume_enables_the_job_and_recomputes_the_next_fire() -> None:
    store = FakeCronStore()
    past_fire = datetime(2020, 1, 1, tzinfo=UTC)
    await store.create(make_job(enabled=False, next_fire_at=past_fire))

    result = await CronResumeTool(store).execute({"job_id": JOB_ID}, CONTEXT)

    assert result.startswith(f"resumed cron job {JOB_ID}")
    assert store.jobs[JOB_ID].enabled
    assert store.jobs[JOB_ID].next_fire_at > utc_now()


@pytest.mark.parametrize("tool_cls", [CronPauseTool, CronResumeTool])
async def test_pause_and_resume_refuse_foreign_jobs(
    tool_cls: type[CronPauseTool | CronResumeTool],
) -> None:
    store = FakeCronStore()
    await store.create(make_job(user_id=OTHER_USER_ID))

    result = await tool_cls(store).execute({"job_id": JOB_ID}, CONTEXT)

    assert result == "error: cron job not found"
