"""Tests for the SQL cron job store (SQLite :memory:)."""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from octoforge_core.cron.api import CronJob, CronJobNotFoundError
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.tasks.models import TaskStatus

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "alice"
USER_B = "bob"
CHANNEL = "web"
OWNER_A = "scheduler-a"
OWNER_B = "scheduler-b"
MISSING_JOB_ID = "no-such-job"
RETRY_TWO = 2

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
CREATED_AT = NOW - timedelta(days=10)
DUE_AT = NOW - timedelta(minutes=5)
FUTURE_AT = NOW + timedelta(hours=1)
FRESH_CLAIM_AT = NOW - timedelta(seconds=10)
STALE_CLAIM_AT = NOW - timedelta(hours=2)
STALE_BEFORE = NOW - timedelta(minutes=1)

BASE_JOB = CronJob(
    id="job-1",
    user_id=USER_A,
    channel=CHANNEL,
    title="morning report",
    schedule="0 9 * * *",
    timezone="UTC",
    prompt="prepare the report",
    enabled=True,
    next_fire_at=DUE_AT,
    last_fire_at=None,
    claimed_by=None,
    claimed_at=None,
    created_at=CREATED_AT,
    one_shot=False,
    last_status=None,
    last_error=None,
    retry_count=0,
)


def make_job(**overrides: object) -> CronJob:
    """Return the base job with the given fields replaced."""
    return replace(BASE_JOB, **overrides)


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemyCronStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield SqlAlchemyCronStore(create_session_factory(engine))
    await engine.dispose()


async def test_create_and_get_roundtrip(store: SqlAlchemyCronStore) -> None:
    created = await store.create(BASE_JOB)

    fetched = await store.get(BASE_JOB.id)

    assert created == BASE_JOB
    assert fetched == BASE_JOB
    assert fetched.next_fire_at.tzinfo == UTC


async def test_get_missing_job_raises(store: SqlAlchemyCronStore) -> None:
    with pytest.raises(CronJobNotFoundError):
        await store.get(MISSING_JOB_ID)


async def test_list_for_user_is_isolated_and_ordered(store: SqlAlchemyCronStore) -> None:
    older = make_job(id="a-older", created_at=CREATED_AT)
    newer = make_job(id="a-newer", created_at=CREATED_AT + timedelta(days=1))
    await store.create(newer)
    await store.create(make_job(id="b-job", user_id=USER_B))
    await store.create(older)

    jobs = await store.list_for_user(USER_A)

    assert [job.id for job in jobs] == [older.id, newer.id]


async def test_delete_for_user_removes_only_the_owned_job(store: SqlAlchemyCronStore) -> None:
    await store.create(BASE_JOB)

    with pytest.raises(CronJobNotFoundError):
        await store.delete_for_user(USER_B, BASE_JOB.id)
    with pytest.raises(CronJobNotFoundError):
        await store.delete_for_user(USER_A, MISSING_JOB_ID)

    await store.delete_for_user(USER_A, BASE_JOB.id)

    with pytest.raises(CronJobNotFoundError):
        await store.get(BASE_JOB.id)


async def test_set_enabled_toggles_and_moves_next_fire(store: SqlAlchemyCronStore) -> None:
    await store.create(BASE_JOB)

    paused = await store.set_enabled(USER_A, BASE_JOB.id, False)

    assert paused.enabled is False
    assert paused.next_fire_at == DUE_AT  # untouched when not passed

    resumed = await store.set_enabled(USER_A, BASE_JOB.id, True, FUTURE_AT)

    assert resumed.enabled is True
    assert resumed.next_fire_at == FUTURE_AT

    with pytest.raises(CronJobNotFoundError):
        await store.set_enabled(USER_B, BASE_JOB.id, False)


async def test_list_due_filters_window_enabled_and_claims(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job(id="due-unclaimed"))
    await store.create(make_job(id="due-later", next_fire_at=NOW - timedelta(minutes=1)))
    await store.create(make_job(id="future", next_fire_at=FUTURE_AT))
    await store.create(make_job(id="disabled", enabled=False))
    await store.create(make_job(id="claimed-fresh", claimed_by=OWNER_A, claimed_at=FRESH_CLAIM_AT))
    await store.create(
        make_job(
            id="claimed-stale",
            next_fire_at=NOW - timedelta(minutes=3),
            claimed_by=OWNER_A,
            claimed_at=STALE_CLAIM_AT,
        )
    )

    due = await store.list_due(NOW, STALE_BEFORE, limit=10)

    assert [job.id for job in due] == ["due-unclaimed", "claimed-stale", "due-later"]


async def test_list_due_respects_the_limit(store: SqlAlchemyCronStore) -> None:
    for index in range(3):
        await store.create(
            make_job(id=f"due-{index}", next_fire_at=DUE_AT + timedelta(minutes=index))
        )

    due = await store.list_due(NOW, STALE_BEFORE, limit=2)

    assert [job.id for job in due] == ["due-0", "due-1"]


async def test_claim_is_a_cas_lease(store: SqlAlchemyCronStore) -> None:
    await store.create(BASE_JOB)

    first = await store.claim(BASE_JOB.id, DUE_AT, OWNER_A, NOW, STALE_BEFORE)
    second = await store.claim(BASE_JOB.id, DUE_AT, OWNER_B, NOW, STALE_BEFORE)

    assert first is True
    assert second is False  # the fresh claim blocks the race loser
    claimed = await store.get(BASE_JOB.id)
    assert claimed.claimed_by == OWNER_A
    assert claimed.claimed_at == NOW


async def test_claim_loses_to_a_concurrent_pause(store: SqlAlchemyCronStore) -> None:
    await store.create(BASE_JOB)
    await store.set_enabled(USER_A, BASE_JOB.id, False)

    claimed = await store.claim(BASE_JOB.id, DUE_AT, OWNER_A, NOW, STALE_BEFORE)

    assert claimed is False  # a pause racing the claim after list_due must win
    assert (await store.get(BASE_JOB.id)).claimed_by is None


async def test_claim_fails_on_a_moved_next_fire(store: SqlAlchemyCronStore) -> None:
    await store.create(BASE_JOB)

    claimed = await store.claim(BASE_JOB.id, FUTURE_AT, OWNER_A, NOW, STALE_BEFORE)

    assert claimed is False


async def test_claim_reclaims_a_stale_lease(store: SqlAlchemyCronStore) -> None:
    await store.create(make_job(claimed_by=OWNER_A, claimed_at=STALE_CLAIM_AT))

    claimed = await store.claim(BASE_JOB.id, DUE_AT, OWNER_B, NOW, STALE_BEFORE)

    assert claimed is True
    assert (await store.get(BASE_JOB.id)).claimed_by == OWNER_B


async def test_release_claim_makes_the_job_claimable_again(store: SqlAlchemyCronStore) -> None:
    await store.create(BASE_JOB)
    await store.claim(BASE_JOB.id, DUE_AT, OWNER_A, NOW, STALE_BEFORE)

    await store.release_claim(BASE_JOB.id)

    released = await store.get(BASE_JOB.id)
    assert released.claimed_by is None
    assert released.claimed_at is None
    reclaimed = await store.claim(BASE_JOB.id, DUE_AT, OWNER_B, NOW, STALE_BEFORE)
    assert reclaimed is True


async def test_complete_fire_bumps_fire_times_and_drops_the_lease(
    store: SqlAlchemyCronStore,
) -> None:
    await store.create(make_job(claimed_by=OWNER_A, claimed_at=FRESH_CLAIM_AT))

    await store.complete_fire(BASE_JOB.id, fired_at=NOW, next_fire_at=FUTURE_AT)

    fired = await store.get(BASE_JOB.id)
    assert fired.last_fire_at == NOW
    assert fired.next_fire_at == FUTURE_AT
    assert fired.claimed_by is None
    assert fired.claimed_at is None


async def test_record_fire_result_with_retry_reschedules_and_grows_the_streak(
    store: SqlAlchemyCronStore,
) -> None:
    await store.create(BASE_JOB)
    retry_at = NOW + timedelta(minutes=1)

    await store.record_fire_result(BASE_JOB.id, TaskStatus.FAILED, "boom", retry_at)
    await store.record_fire_result(BASE_JOB.id, TaskStatus.FAILED, "boom2", retry_at)

    updated = await store.get(BASE_JOB.id)
    assert updated.last_status is TaskStatus.FAILED
    assert updated.last_error == "boom2"
    assert updated.next_fire_at == retry_at
    assert updated.retry_count == RETRY_TWO


async def test_record_fire_result_without_retry_keeps_the_slot_and_resets_the_streak(
    store: SqlAlchemyCronStore,
) -> None:
    await store.create(make_job(retry_count=RETRY_TWO))

    await store.record_fire_result(BASE_JOB.id, TaskStatus.DONE, None, None)

    updated = await store.get(BASE_JOB.id)
    assert updated.last_status is TaskStatus.DONE
    assert updated.last_error is None
    assert updated.next_fire_at == DUE_AT  # the schedule slot is untouched
    assert updated.retry_count == 0
