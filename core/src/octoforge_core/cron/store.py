"""SQL adapter for the public cron store port."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.cron._job_store import CronJobStore
from octoforge_core.cron._lease_store import CronLeaseStore
from octoforge_core.cron._queries import CronQueries
from octoforge_core.cron.types import (
    CronClaim,
    CronEnablement,
    CronFireResult,
    CronJob,
)


class SqlAlchemyCronStore:
    """Persist cron jobs and serialize lease state transitions in SQL."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._queries = CronQueries(session_factory)
        self._jobs = CronJobStore(session_factory)
        self._leases = CronLeaseStore(session_factory)

    async def create(self, job: CronJob) -> CronJob:
        return await self._jobs.create(job)

    async def get(self, job_id: str) -> CronJob:
        return await self._queries.get(job_id)

    async def list_for_user(self, user_id: str) -> list[CronJob]:
        return await self._queries.list_for_user(user_id)

    async def delete_for_user(self, user_id: str, job_id: str) -> None:
        await self._jobs.delete_for_user(user_id, job_id)

    async def set_enabled(self, request: CronEnablement) -> CronJob:
        return await self._jobs.set_enabled(request)

    async def list_due(self, now: datetime, stale_before: datetime, limit: int) -> list[CronJob]:
        return await self._queries.list_due(now, stale_before, limit)

    async def claim(self, request: CronClaim) -> bool:
        return await self._leases.claim(request)

    async def release_claim(self, job_id: str) -> None:
        await self._leases.release(job_id)

    async def complete_fire(self, job_id: str, fired_at: datetime, next_fire_at: datetime) -> None:
        await self._leases.complete(job_id, fired_at, next_fire_at)

    async def record_fire_result(self, result: CronFireResult) -> None:
        await self._leases.record_result(result)
