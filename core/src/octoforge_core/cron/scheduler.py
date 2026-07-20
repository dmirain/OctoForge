"""Asyncio scheduler loop: claims due jobs and fires them into dialogs.

One shot per due job per tick: missed runs are coalesced into a single wake
whose prompt carries the missed-run count, and `next_fire_at` is recomputed
from the fire time, so a long downtime never turns into a burst. Catching up
is additionally paced by a stagger between shots and bounded by the replay
limit (openclaw-review item 5).
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

    `owner` is a unique id of this scheduler instance (or test); it lands in
    `claimed_by` so a dead owner's lease can expire (`lease_ttl_seconds`) and
    be reclaimed by a live one.
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
        """Poll until cancelled: one tick per poll interval."""
        while True:
            await self.tick()
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self, now: datetime | None = None) -> None:
        """Run one scheduling pass; `now` is injectable for tests."""
        fired_at = now if now is not None else utc_now()
        stale_before = fired_at - timedelta(seconds=self._lease_ttl_seconds)
        due = await self._store.list_due(fired_at, stale_before, self._replay_limit)
        for index, job in enumerate(due):
            if index > 0:
                await asyncio.sleep(REPLAY_STAGGER_SECONDS)  # pace the catch-up burst
            await self._fire(job, fired_at, stale_before)

    async def _fire(self, job: CronJob, now: datetime, stale_before: datetime) -> None:
        claimed = await self._store.claim(job.id, job.next_fire_at, self._owner, now, stale_before)
        if not claimed:
            return  # another scheduler instance won the CAS race
        try:
            await self._waker.wake(
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
        await self._store.complete_fire(
            job.id,
            fired_at=now,
            next_fire_at=compute_next_fire(job.schedule, job.timezone, now),
        )

    def _prompt_for(self, job: CronJob, now: datetime) -> str:
        since = job.last_fire_at if job.last_fire_at is not None else job.created_at
        missed = count_missed(job.schedule, job.timezone, since, now)
        if missed == 0:
            return job.prompt
        return job.prompt + MISSED_RUNS_SUFFIX_TEMPLATE.format(count=missed)
