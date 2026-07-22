"""Tests for the cron scheduler (real SQL store, recording waker) and schedule math."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from octoforge_core.cron import scheduler as scheduler_module
from octoforge_core.cron.api import (
    CronJob,
    CronScheduleError,
    Scheduler,
    compute_next_fire,
    count_missed,
)
from octoforge_core.cron.scheduler import CronScheduler, CronSchedulerConfig
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.db.engine import create_engine, create_session_factory, init_db

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "alice"
CHANNEL = "web"
OWNER = "scheduler-test"
OTHER_OWNER = "scheduler-other"
WAKE_FAILURE = "wake delivery failed"
POLL_INTERVAL_SECONDS = 0.01
LEASE_TTL_SECONDS = 60.0
REPLAY_LIMIT = 5
SMALL_REPLAY_LIMIT = 2
THREE_JOBS = 3
TWO_CALLS = 2
MISSED_TWO = 2
MISSED_CAP = 5
DAILY_9AM = "0 9 * * *"
EVERY_MINUTE = "* * * * *"

NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
CREATED_AT = NOW - timedelta(minutes=10)
DUE_AT = NOW - timedelta(minutes=5)
NEXT_9AM = datetime(2026, 7, 19, 9, 0, tzinfo=UTC)
STALE_BEFORE = NOW - timedelta(seconds=LEASE_TTL_SECONDS)

BASE_JOB = CronJob(
    id="job-1",
    user_id=USER_A,
    channel=CHANNEL,
    title="morning report",
    schedule=DAILY_9AM,
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


@dataclass(frozen=True, slots=True)
class WakeCall:
    """One delivered cron firing."""

    user_id: str
    channel: str
    title: str
    prompt: str
    cron_job_id: str


class RecordingWaker:
    """CronWaker stub recording delivered wakes; can be told to fail or skip (limit hit)."""

    def __init__(self) -> None:
        self.calls: list[WakeCall] = []
        self.fail = False
        self.skip = False

    async def wake(
        self,
        user_id: str,
        channel: str,
        title: str,
        prompt: str,
        cron_job_id: str,
    ) -> bool:
        if self.fail:
            raise RuntimeError(WAKE_FAILURE)
        self.calls.append(
            WakeCall(
                user_id=user_id,
                channel=channel,
                title=title,
                prompt=prompt,
                cron_job_id=cron_job_id,
            )
        )
        return not self.skip


def make_job(**overrides: object) -> CronJob:
    """Return the base job with the given fields replaced."""
    return replace(BASE_JOB, **overrides)


def make_scheduler(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    replay_limit: int = REPLAY_LIMIT,
) -> CronScheduler:
    return CronScheduler(
        store=store,
        waker=waker,
        owner=OWNER,
        config=CronSchedulerConfig(
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            lease_ttl_seconds=LEASE_TTL_SECONDS,
            replay_limit=replay_limit,
        ),
    )


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemyCronStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield SqlAlchemyCronStore(create_session_factory(engine))
    await engine.dispose()


@pytest.fixture
def waker() -> RecordingWaker:
    return RecordingWaker()


@pytest.fixture
def no_stagger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the catch-up pacing so multi-shot ticks stay instant."""
    monkeypatch.setattr(scheduler_module, "REPLAY_STAGGER_SECONDS", 0)


async def test_due_job_fires_and_reschedules(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    await store.create(BASE_JOB)

    await make_scheduler(store, waker).tick(now=NOW)

    assert waker.calls == [
        WakeCall(
            user_id=USER_A,
            channel=CHANNEL,
            title=BASE_JOB.title,
            prompt=BASE_JOB.prompt,
            cron_job_id=BASE_JOB.id,
        )
    ]
    fired = await store.get(BASE_JOB.id)
    assert fired.last_fire_at == NOW
    assert fired.next_fire_at == NEXT_9AM  # recomputed from the fire time
    assert fired.claimed_by is None
    assert fired.claimed_at is None


async def test_future_and_disabled_jobs_do_not_fire(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    await store.create(make_job(id="future", next_fire_at=NOW + timedelta(hours=1)))
    await store.create(make_job(id="disabled", enabled=False))

    await make_scheduler(store, waker).tick(now=NOW)

    assert waker.calls == []
    assert (await store.get("future")).last_fire_at is None
    assert (await store.get("disabled")).next_fire_at == DUE_AT


async def test_missed_runs_are_coalesced_into_one_shot(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    last_fire = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    await store.create(
        make_job(
            last_fire_at=last_fire,
            next_fire_at=datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
            created_at=CREATED_AT,
        )
    )

    await make_scheduler(store, waker).tick(now=NOW)

    assert len(waker.calls) == 1  # three due slots collapsed into a single wake
    call = waker.calls[0]
    assert call.prompt.startswith(BASE_JOB.prompt)
    assert f"[{MISSED_TWO} scheduled runs were missed and coalesced into this one.]" in call.prompt
    assert (await store.get(BASE_JOB.id)).next_fire_at == NEXT_9AM


async def test_replay_limit_defers_the_remaining_jobs(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    for index in range(THREE_JOBS):
        await store.create(make_job(id=f"due-{index}"))

    scheduler = make_scheduler(store, waker, replay_limit=SMALL_REPLAY_LIMIT)
    await scheduler.tick(now=NOW)

    assert len(waker.calls) == SMALL_REPLAY_LIMIT
    leftover = await store.get("due-2")
    assert leftover.last_fire_at is None
    assert leftover.next_fire_at == DUE_AT  # still due, waiting for a later tick

    await scheduler.tick(now=NOW)

    assert len(waker.calls) == THREE_JOBS


async def test_failed_wake_releases_the_claim(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    await store.create(BASE_JOB)
    scheduler = make_scheduler(store, waker)
    waker.fail = True

    await scheduler.tick(now=NOW)

    assert waker.calls == []
    job = await store.get(BASE_JOB.id)
    assert job.claimed_by is None  # released, so the job stays claimable
    assert job.last_fire_at is None
    assert job.next_fire_at == DUE_AT

    waker.fail = False
    await scheduler.tick(now=NOW)

    assert len(waker.calls) == 1


async def test_wake_skipped_by_limit_keeps_the_claim_without_advancing_schedule(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    """A `wake()` that returns False (process limit hit) must not be treated as a fire.

    Regression: advancing `next_fire_at` here would silently skip a one-shot
    reminder to its next schedule slot (up to a year away for a dated
    expression) even though no process was ever started.
    """
    await store.create(BASE_JOB)
    waker.skip = True

    await make_scheduler(store, waker).tick(now=NOW)

    assert len(waker.calls) == 1  # wake was attempted...
    job = await store.get(BASE_JOB.id)
    assert job.last_fire_at is None  # ...but not counted as a completed fire
    assert job.next_fire_at == DUE_AT  # schedule untouched, still due
    assert job.claimed_by == OWNER  # claim kept, retried once the lease goes stale

    waker.skip = False
    await make_scheduler(store, waker).tick(now=NOW + timedelta(seconds=LEASE_TTL_SECONDS + 1))

    assert len(waker.calls) == TWO_CALLS
    assert (await store.get(BASE_JOB.id)).next_fire_at == NEXT_9AM


class FlakyCompleteFireStore:
    """Delegates to a real store, but `complete_fire` raises on the first call."""

    def __init__(self, inner: SqlAlchemyCronStore) -> None:
        self._inner = inner
        self.complete_fire_calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    async def complete_fire(self, job_id: str, fired_at: datetime, next_fire_at: datetime) -> None:
        self.complete_fire_calls += 1
        if self.complete_fire_calls == 1:
            raise RuntimeError("db hiccup")
        await self._inner.complete_fire(job_id, fired_at=fired_at, next_fire_at=next_fire_at)


async def test_complete_fire_failure_does_not_kill_the_tick_or_lose_the_job(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    """A `complete_fire` failure after a successful wake must not crash the scheduler.

    Regression: an unguarded `complete_fire` call used to propagate out of
    `_fire`/`tick`, and since `run_forever` had no catch-all either, one
    transient store error during bookkeeping would permanently stop the
    scheduler loop for every user's cron jobs.
    """
    flaky_store = FlakyCompleteFireStore(store)
    await store.create(BASE_JOB)
    scheduler = make_scheduler(flaky_store, waker)  # type: ignore[arg-type]

    await scheduler.tick(now=NOW)  # complete_fire raises, must not propagate

    assert len(waker.calls) == 1
    job = await store.get(BASE_JOB.id)
    assert job.claimed_by == OWNER  # bookkeeping failed, claim kept for a stale-reclaim retry
    assert job.next_fire_at == DUE_AT  # not advanced: complete_fire never committed

    await scheduler.tick(now=NOW + timedelta(seconds=LEASE_TTL_SECONDS + 1))

    assert len(waker.calls) == TWO_CALLS  # reclaimed once stale, complete_fire now succeeds
    assert (await store.get(BASE_JOB.id)).next_fire_at == NEXT_9AM


async def test_run_forever_survives_a_tick_failure(
    waker: RecordingWaker,
) -> None:
    class ExplodingStore:
        async def list_due(self, *args: object, **kwargs: object) -> list[CronJob]:
            raise RuntimeError("store outage")

    scheduler = CronScheduler(
        store=ExplodingStore(),  # type: ignore[arg-type]
        waker=waker,
        owner=OWNER,
        config=CronSchedulerConfig(
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            lease_ttl_seconds=LEASE_TTL_SECONDS,
            replay_limit=REPLAY_LIMIT,
        ),
    )
    task = asyncio.create_task(scheduler.run_forever())
    await asyncio.sleep(POLL_INTERVAL_SECONDS * 3)

    assert not task.done()  # the loop kept polling despite the exception

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_a_fresh_claim_by_another_owner_skips_the_job(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
    no_stagger: None,
) -> None:
    await store.create(BASE_JOB)
    await store.claim(BASE_JOB.id, DUE_AT, OTHER_OWNER, NOW, STALE_BEFORE)

    await make_scheduler(store, waker).tick(now=NOW)

    assert waker.calls == []
    assert (await store.get(BASE_JOB.id)).claimed_by == OTHER_OWNER


# --- schedule math -----------------------------------------------------------


def test_compute_next_fire_uses_the_job_timezone() -> None:
    base = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)

    next_fire = compute_next_fire(DAILY_9AM, "Europe/Moscow", base)

    # 09:00 in Moscow (UTC+3, no DST) is 06:00 UTC of the same day
    assert next_fire == datetime(2026, 7, 18, 6, 0, tzinfo=UTC)


def test_compute_next_fire_moves_to_the_next_day_once_past() -> None:
    base = datetime(2026, 7, 18, 6, 30, tzinfo=UTC)

    next_fire = compute_next_fire(DAILY_9AM, "Europe/Moscow", base)

    assert next_fire == datetime(2026, 7, 19, 6, 0, tzinfo=UTC)


def test_compute_next_fire_rejects_invalid_input() -> None:
    base = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)

    with pytest.raises(CronScheduleError):
        compute_next_fire("not a cron", "UTC", base)
    with pytest.raises(CronScheduleError):
        compute_next_fire(DAILY_9AM, "Mars/Olympus_Mons", base)


def test_count_missed_counts_slots_minus_the_current_shot() -> None:
    since = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)

    assert count_missed(DAILY_9AM, "UTC", since, NOW) == MISSED_TWO


def test_count_missed_is_zero_for_an_on_time_job() -> None:
    since = datetime(2026, 7, 17, 9, 0, tzinfo=UTC)

    assert count_missed(DAILY_9AM, "UTC", since, NOW) == 0


def test_count_missed_is_capped() -> None:
    since = NOW - timedelta(hours=1)  # 60 minutely slots

    assert count_missed(EVERY_MINUTE, "UTC", since, NOW, cap=MISSED_CAP) == MISSED_CAP


def test_count_missed_rejects_invalid_input() -> None:
    with pytest.raises(CronScheduleError):
        count_missed("bogus", "UTC", CREATED_AT, NOW)


class ManualScheduler:
    """Alternative Scheduler engine: counts the run_forever invocations."""

    def __init__(self) -> None:
        self.runs = 0

    async def run_forever(self) -> None:
        self.runs += 1


async def test_cron_scheduler_implements_the_scheduler_port(
    store: SqlAlchemyCronStore,
    waker: RecordingWaker,
) -> None:
    scheduler: Scheduler = make_scheduler(store, waker)

    assert isinstance(scheduler, Scheduler)


async def test_scheduler_port_accepts_alternative_engines() -> None:
    engine = ManualScheduler()
    scheduler: Scheduler = engine

    await scheduler.run_forever()

    assert engine.runs == 1
