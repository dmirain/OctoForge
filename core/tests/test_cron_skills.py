"""Tests for the native cron job skills (no HTTP API involved)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from octoforge_core.cron.api import CronJob, CronJobNotFoundError
from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.basic.cron_jobs import (
    CronCreateSkill,
    CronDeleteSkill,
    CronListSkill,
    CronPauseSkill,
    CronResumeSkill,
)
from octoforge_core.tasks.models import TaskStatus
from octoforge_core.time import utc_now

USER_ID = "alice"
OTHER_USER_ID = "bob"
CHANNEL = "telegram"
DIALOG_ID = "dialog-1"
JOB_ID = "job-1"
MISSING_JOB_ID = "no-such-job"

VALID_SCHEDULE = "0 9 * * *"
VALID_TIMEZONE = "Europe/Moscow"
CONTEXT = SkillContext(user_id=USER_ID, channel=CHANNEL, dialog_id=DIALOG_ID)
EXPECTED_TWO_JOBS = 2
RETRY_TWO = 2


class FakeCronStore:
    """In-memory CronStore; lease methods are irrelevant to the skills."""

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

    async def set_enabled(
        self,
        user_id: str,
        job_id: str,
        enabled: bool,
        next_fire_at: datetime | None = None,
    ) -> CronJob:
        job = await self._get_owned(user_id, job_id)
        updated = replace(
            job,
            enabled=enabled,
            next_fire_at=next_fire_at if next_fire_at is not None else job.next_fire_at,
        )
        self.jobs[job_id] = updated
        return updated

    async def _get_owned(self, user_id: str, job_id: str) -> CronJob:
        job = await self.get(job_id)
        if job.user_id != user_id:
            raise CronJobNotFoundError(job_id)
        return job

    async def list_due(self, now: datetime, stale_before: datetime, limit: int) -> list[CronJob]:
        raise NotImplementedError

    async def claim(
        self,
        job_id: str,
        expected_next_fire_at: datetime,
        owner: str,
        now: datetime,
        stale_before: datetime,
    ) -> bool:
        raise NotImplementedError

    async def release_claim(self, job_id: str) -> None:
        raise NotImplementedError

    async def complete_fire(self, job_id: str, fired_at: datetime, next_fire_at: datetime) -> None:
        raise NotImplementedError

    async def record_fire_result(
        self,
        job_id: str,
        status: TaskStatus,
        error: str | None,
        retry_at: datetime | None,
    ) -> None:
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


def create_arguments(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "title": "morning report",
        "schedule": VALID_SCHEDULE,
        "prompt": "good morning",
        "timezone": VALID_TIMEZONE,
    }
    return arguments | overrides


async def test_create_stores_an_enabled_job_for_the_caller() -> None:
    store = FakeCronStore()

    result = await CronCreateSkill(store).execute(create_arguments(), CONTEXT)

    assert "created cron job" in result
    (job,) = store.jobs.values()
    assert job.user_id == USER_ID
    assert job.channel == CHANNEL
    assert job.enabled
    assert job.next_fire_at > utc_now()
    assert job.id in result


async def test_create_rejects_an_invalid_schedule() -> None:
    store = FakeCronStore()

    result = await CronCreateSkill(store).execute(create_arguments(schedule="not a cron"), CONTEXT)

    assert result.startswith("error:")
    assert store.jobs == {}


async def test_create_rejects_an_unknown_timezone() -> None:
    store = FakeCronStore()

    result = await CronCreateSkill(store).execute(
        create_arguments(timezone="Mars/Olympus"), CONTEXT
    )

    assert result.startswith("error:")
    assert store.jobs == {}


async def test_list_reports_only_the_callers_jobs() -> None:
    store = FakeCronStore()
    await store.create(make_job())
    await store.create(make_job(id="job-2", user_id=OTHER_USER_ID))

    result = await CronListSkill(store).execute({}, CONTEXT)

    assert JOB_ID in result
    assert "job-2" not in result
    assert "[enabled]" in result


async def test_list_reports_when_there_are_no_jobs() -> None:
    result = await CronListSkill(FakeCronStore()).execute({}, CONTEXT)

    assert result == "no cron jobs"


async def test_delete_removes_the_job() -> None:
    store = FakeCronStore()
    await store.create(make_job())

    result = await CronDeleteSkill(store).execute({"job_id": JOB_ID}, CONTEXT)

    assert result == f"deleted cron job {JOB_ID}"
    assert store.jobs == {}


async def test_delete_refuses_foreign_and_missing_jobs() -> None:
    store = FakeCronStore()
    await store.create(make_job(user_id=OTHER_USER_ID))

    foreign = await CronDeleteSkill(store).execute({"job_id": JOB_ID}, CONTEXT)
    missing = await CronDeleteSkill(store).execute({"job_id": MISSING_JOB_ID}, CONTEXT)

    assert foreign == missing == "error: cron job not found"
    assert JOB_ID in store.jobs


async def test_pause_disables_the_job() -> None:
    store = FakeCronStore()
    await store.create(make_job())

    result = await CronPauseSkill(store).execute({"job_id": JOB_ID}, CONTEXT)

    assert result.startswith(f"paused cron job {JOB_ID}")
    assert not store.jobs[JOB_ID].enabled


async def test_resume_enables_the_job_and_recomputes_the_next_fire() -> None:
    store = FakeCronStore()
    past_fire = datetime(2020, 1, 1, tzinfo=UTC)
    await store.create(make_job(enabled=False, next_fire_at=past_fire))

    result = await CronResumeSkill(store).execute({"job_id": JOB_ID}, CONTEXT)

    assert result.startswith(f"resumed cron job {JOB_ID}")
    assert store.jobs[JOB_ID].enabled
    assert store.jobs[JOB_ID].next_fire_at > utc_now()


@pytest.mark.parametrize("skill_cls", [CronPauseSkill, CronResumeSkill])
async def test_pause_and_resume_refuse_foreign_jobs(
    skill_cls: type[CronPauseSkill | CronResumeSkill],
) -> None:
    store = FakeCronStore()
    await store.create(make_job(user_id=OTHER_USER_ID))

    result = await skill_cls(store).execute({"job_id": JOB_ID}, CONTEXT)

    assert result == "error: cron job not found"


async def test_create_is_idempotent_for_an_identical_job() -> None:
    store = FakeCronStore()
    skill = CronCreateSkill(store)

    first = await skill.execute(create_arguments(), CONTEXT)
    second = await skill.execute(create_arguments(), CONTEXT)

    assert "created cron job" in first
    assert "already exists" in second
    assert len(store.jobs) == 1


async def test_create_with_a_different_one_shot_is_not_a_duplicate() -> None:
    store = FakeCronStore()
    skill = CronCreateSkill(store)

    await skill.execute(create_arguments(), CONTEXT)
    result = await skill.execute(create_arguments(one_shot=True), CONTEXT)

    assert "created cron job" in result
    assert len(store.jobs) == EXPECTED_TWO_JOBS


async def test_create_one_shot_marks_the_job() -> None:
    store = FakeCronStore()

    result = await CronCreateSkill(store).execute(create_arguments(one_shot=True), CONTEXT)

    (job,) = store.jobs.values()
    assert job.one_shot
    assert "one-shot" in result


async def test_list_shows_the_last_run_and_retry_streak() -> None:
    store = FakeCronStore()
    await store.create(
        make_job(
            last_status=TaskStatus.FAILED,
            last_error="iteration limit reached",
            retry_count=RETRY_TWO,
        )
    )

    result = await CronListSkill(store).execute({}, CONTEXT)

    assert "last run: failed (iteration limit reached)" in result
    assert "retry #2" in result
