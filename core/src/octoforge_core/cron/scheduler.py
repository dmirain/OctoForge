"""Claim due cron jobs and coalesce downtime into paced single wakes."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from octoforge_core.cron.api import (
    CronClaim,
    CronJob,
    CronStore,
    CronWake,
    CronWaker,
    WakeOutcome,
    compute_next_fire,
)
from octoforge_core.cron.schedule import missed_run_count
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

REPLAY_STAGGER_SECONDS = 0.5
MISSED_RUNS_SUFFIX_TEMPLATE = "\n[{count} scheduled runs were missed and coalesced into this one.]"


@dataclass(frozen=True, slots=True)
class CronSchedulerTiming:
    poll_interval_seconds: float
    lease_ttl_seconds: float
    replay_limit: int


@dataclass(frozen=True, slots=True)
class CronSchedulerConfig:
    owner: str
    timing: CronSchedulerTiming


class CronScheduler:
    def __init__(
        self,
        store: CronStore,
        waker: CronWaker,
        config: CronSchedulerConfig,
    ) -> None:
        self._store = store
        self._waker = waker
        self._owner = config.owner
        self._poll_interval_seconds = config.timing.poll_interval_seconds
        self._lease_ttl_seconds = config.timing.lease_ttl_seconds
        self._replay_limit = config.timing.replay_limit

    async def run_forever(self) -> None:
        """Poll until cancelled, logging a failed tick and retrying the next one."""
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("cron scheduler tick failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def tick(self, now: datetime | None = None) -> None:
        fired_at = now if now is not None else utc_now()
        stale_before = fired_at - timedelta(seconds=self._lease_ttl_seconds)
        due = await self._store.list_due(fired_at, stale_before, self._replay_limit)
        for index, job in enumerate(due):
            # Pace downtime catch-up, not independent jobs due on the same tick.
            if index > 0 and missed_run_count(job, fired_at) > 0:
                await asyncio.sleep(REPLAY_STAGGER_SECONDS)
            await self._fire(job, fired_at, stale_before)

    async def _fire(self, job: CronJob, now: datetime, stale_before: datetime) -> None:
        claimed = await self._store.claim(
            CronClaim(job.id, job.next_fire_at, self._owner, now, stale_before)
        )
        if not claimed:
            return  # another scheduler instance won the CAS race
        try:
            delivered = await self._waker.wake(
                CronWake(
                    job.user_id,
                    job.channel,
                    job.title,
                    self._prompt_for(job, now),
                    job.id,
                )
            )
        except Exception:
            logger.exception("cron wake delivery failed: job=%s user=%s", job.id, job.user_id)
            await self._store.release_claim(job.id)
            return
        if delivered is WakeOutcome.NOT_OURS:
            # The instance owning this dialog will fire it on its next tick.
            await self._store.release_claim(job.id)
            return
        if delivered is not WakeOutcome.DELIVERED:
            # Keep the lease: advancing skips the fire, releasing retries every tick.
            return
        try:
            await self._store.complete_fire(
                job.id,
                fired_at=now,
                next_fire_at=compute_next_fire(job.schedule, job.timezone, now),
            )
        except Exception:
            logger.exception(
                "cron complete_fire failed after successful delivery: job=%s user=%s",
                job.id,
                job.user_id,
            )

    def _prompt_for(self, job: CronJob, now: datetime) -> str:
        missed = missed_run_count(job, now)
        if missed == 0:
            return job.prompt
        return job.prompt + MISSED_RUNS_SUFFIX_TEMPLATE.format(count=missed)
