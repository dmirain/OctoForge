"""Asyncio scheduler loop: claims due jobs and fires them into dialogs.

One shot per due job per tick: missed runs are coalesced into a single wake
whose prompt carries the missed-run count, and `next_fire_at` is recomputed
from the fire time, so a long downtime never turns into a burst. Catching up
is additionally paced by a stagger before each job that itself missed one or
more runs, and bounded by the replay limit (openclaw-review item 5).
`REPLAY_STAGGER_SECONDS` is scoped to that catch-up case on purpose: several
independent jobs simply due at the same on-time tick (e.g. minute-aligned
schedules lining up) are not a downtime burst and fire back-to-back.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from octoforge_core.cron.api import (
    CronJob,
    CronStore,
    CronWaker,
    compute_next_fire,
    count_missed,
)
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

REPLAY_STAGGER_SECONDS = 0.5
MISSED_RUNS_SUFFIX_TEMPLATE = "\n[{count} scheduled runs were missed and coalesced into this one.]"


@dataclass(frozen=True, slots=True)
class CronSchedulerConfig:
    """Tuning knobs of the scheduler loop (from the composition-root settings)."""

    poll_interval_seconds: float
    lease_ttl_seconds: float
    replay_limit: int


class CronScheduler:
    """Polling scheduler with lease-based exactly-once firing.

    Implements the `Scheduler` port from `cron/api.py`. `owner` is a unique
    id of this scheduler instance (or test); it lands in `claimed_by` so a
    dead owner's lease can expire (`lease_ttl_seconds`) and be reclaimed by
    a live one.
    """

    def __init__(
        self,
        store: CronStore,
        waker: CronWaker,
        owner: str,
        config: CronSchedulerConfig,
    ) -> None:
        self._store = store
        self._waker = waker
        self._owner = owner
        self._poll_interval_seconds = config.poll_interval_seconds
        self._lease_ttl_seconds = config.lease_ttl_seconds
        self._replay_limit = config.replay_limit

    async def run_forever(self) -> None:
        """Poll until cancelled: one tick per poll interval.

        A tick failure (store outage, unexpected error) must not kill the
        loop forever — every user's cron would stop firing until a process
        restart. Log and retry on the next poll instead.
        """
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("cron scheduler tick failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self, now: datetime | None = None) -> None:
        """Run one scheduling pass; `now` is injectable for tests."""
        fired_at = now if now is not None else utc_now()
        stale_before = fired_at - timedelta(seconds=self._lease_ttl_seconds)
        due = await self._store.list_due(fired_at, stale_before, self._replay_limit)
        for index, job in enumerate(due):
            # pace only a downtime catch-up burst (this job itself missed one
            # or more runs) — several independent jobs simply due at the same
            # on-time tick must fire back-to-back, no tax for coinciding
            if index > 0 and _has_missed_runs(job, fired_at):
                await asyncio.sleep(REPLAY_STAGGER_SECONDS)
            await self._fire(job, fired_at, stale_before)

    async def _fire(self, job: CronJob, now: datetime, stale_before: datetime) -> None:
        claimed = await self._store.claim(job.id, job.next_fire_at, self._owner, now, stale_before)
        if not claimed:
            return  # another scheduler instance won the CAS race
        try:
            delivered = await self._waker.wake(
                user_id=job.user_id,
                channel=job.channel,
                title=job.title,
                prompt=self._prompt_for(job, now),
                cron_job_id=job.id,
            )
        except Exception:
            # delivery failed: free the lease so the job can fire on a later tick
            logger.exception("cron wake delivery failed: job=%s user=%s", job.id, job.user_id)
            await self._store.release_claim(job.id)
            return
        if not delivered:
            # process limit was hit: no process was started, so this was not a
            # real fire. Leave the claim in place rather than advancing
            # next_fire_at (which would silently skip the job to its next
            # schedule slot, up to a year away for a dated one-shot
            # expression) or releasing it (which would retry every poll tick
            # and spam the limit note). It naturally retries once the lease
            # goes stale.
            return
        try:
            await self._store.complete_fire(
                job.id,
                fired_at=now,
                next_fire_at=compute_next_fire(job.schedule, job.timezone, now),
            )
        except Exception:
            # bookkeeping failed after a successful delivery: don't let this
            # kill the scheduler loop (run_forever also guards, but fail late
            # here too so a partial tick doesn't abort the remaining due
            # jobs). The claim stays in place and is reclaimed once stale.
            logger.exception(
                "cron complete_fire failed after successful delivery: job=%s user=%s",
                job.id,
                job.user_id,
            )

    def _prompt_for(self, job: CronJob, now: datetime) -> str:
        missed = _missed_run_count(job, now)
        if missed == 0:
            return job.prompt
        return job.prompt + MISSED_RUNS_SUFFIX_TEMPLATE.format(count=missed)


def _missed_run_count(job: CronJob, now: datetime) -> int:
    since = job.last_fire_at if job.last_fire_at is not None else job.created_at
    return count_missed(job.schedule, job.timezone, since, now)


def _has_missed_runs(job: CronJob, now: datetime) -> bool:
    """Whether this job's fire is a downtime catch-up (it missed one or more runs)."""
    return _missed_run_count(job, now) > 0
