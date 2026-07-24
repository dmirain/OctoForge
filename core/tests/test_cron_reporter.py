"""Tests for CronOutcomeReporter: outcome recording and one-shot deletion."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from octoforge_core.cron.api import CronJob, CronJobNotFoundError
from octoforge_core.cron.reporter import CronOutcomeReporter
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "alice"
CHANNEL = "web"
DIALOG_ID = "dialog-1"
JOB_ID = "job-1"
ERROR_TEXT = "iteration limit reached"
LONG_ERROR_LEN = 1000
TRUNCATED_ERROR_LEN = 500

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
NEXT_SLOT = NOW + timedelta(days=1)


def make_job(**overrides: object) -> CronJob:
    base = CronJob(
        id=JOB_ID,
        user_id=USER_A,
        channel=CHANNEL,
        title="morning report",
        schedule="0 9 * * *",
        timezone="UTC",
        prompt="prepare the report",
        enabled=True,
        next_fire_at=NEXT_SLOT,
        last_fire_at=NOW,
        claimed_by=None,
        claimed_at=None,
        created_at=NOW - timedelta(days=10),
        one_shot=False,
        last_status=None,
        last_error=None,
        retry_count=0,
    )
    return replace(base, **overrides)


def make_task(error: str | None = None, cron_job_id: object = JOB_ID) -> Task:
    return Task(
        dialog_id=DIALOG_ID,
        user_id=USER_A,
        channel=CHANNEL,
        title="morning report",
        kind=TaskKind.RUN,
        input={} if cron_job_id is None else {"cron_job_id": cron_job_id},
        error=error,
    )


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemyCronStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield SqlAlchemyCronStore(create_session_factory(engine))
    await engine.dispose()


def make_reporter(store: SqlAlchemyCronStore) -> CronOutcomeReporter:
    return CronOutcomeReporter(store)


async def test_done_is_recorded_without_touching_the_schedule(
    store: SqlAlchemyCronStore,
) -> None:
    await store.create(make_job())

    await make_reporter(store).report_outcome(make_task(), TaskStatus.DONE)

    updated = await store.get(JOB_ID)
    assert updated.last_status is TaskStatus.DONE
    assert updated.last_error is None
    assert updated.retry_count == 0
    assert updated.next_fire_at == NEXT_SLOT  # the schedule slot is untouched


async def test_done_deletes_a_one_shot_job(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job(one_shot=True))

    await make_reporter(store).report_outcome(make_task(), TaskStatus.DONE)

    with pytest.raises(CronJobNotFoundError):
        await store.get(JOB_ID)


async def test_failed_is_recorded_without_a_retry(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job())

    await make_reporter(store).report_outcome(make_task(error=ERROR_TEXT), TaskStatus.FAILED)

    updated = await store.get(JOB_ID)
    assert updated.last_status is TaskStatus.FAILED
    assert updated.last_error == ERROR_TEXT
    assert updated.retry_count == 0
    assert updated.next_fire_at == NEXT_SLOT  # no retry: the schedule slot stays


async def test_failed_deletes_a_one_shot_job(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job(one_shot=True))

    await make_reporter(store).report_outcome(make_task(error=ERROR_TEXT), TaskStatus.FAILED)

    with pytest.raises(CronJobNotFoundError):
        await store.get(JOB_ID)


async def test_cancelled_keeps_a_one_shot_job(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job(one_shot=True))

    await make_reporter(store).report_outcome(make_task(), TaskStatus.CANCELLED)

    updated = await store.get(JOB_ID)  # cancelled is recorded, not an attempt
    assert updated.last_status is TaskStatus.CANCELLED
    assert updated.next_fire_at == NEXT_SLOT


async def test_outcome_for_a_deleted_job_is_ignored(store: SqlAlchemyCronStore) -> None:
    await make_reporter(store).report_outcome(make_task(), TaskStatus.DONE)  # no job stored

    with pytest.raises(CronJobNotFoundError):
        await store.get(JOB_ID)


async def test_task_without_cron_tag_is_ignored(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job())

    await make_reporter(store).report_outcome(make_task(cron_job_id=None), TaskStatus.DONE)

    assert (await store.get(JOB_ID)).last_status is None


async def test_long_error_is_truncated(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job())

    await make_reporter(store).report_outcome(
        make_task(error="x" * LONG_ERROR_LEN),
        TaskStatus.FAILED,
    )

    assert len((await store.get(JOB_ID)).last_error or "") == TRUNCATED_ERROR_LEN
