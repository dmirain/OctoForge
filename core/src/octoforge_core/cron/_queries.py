"""Cron job reads with stable ordering and lease visibility."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.cron._rows import claimable, to_cron_job
from octoforge_core.cron.models import CronJobRow
from octoforge_core.cron.types import CronJob, CronJobNotFoundError
from octoforge_core.db.unit_of_work import read_session


class CronQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, job_id: str) -> CronJob:
        async with read_session(self._session_factory) as session:
            row = await session.get(CronJobRow, job_id)
            if row is None:
                raise CronJobNotFoundError(f"cron job '{job_id}' not found")
            return to_cron_job(row)

    async def list_for_user(self, user_id: str) -> list[CronJob]:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(CronJobRow)
                .where(CronJobRow.user_id == user_id)
                .order_by(CronJobRow.created_at, CronJobRow.id)
            )
            return [to_cron_job(row) for row in rows.all()]

    async def list_due(self, now: datetime, stale_before: datetime, limit: int) -> list[CronJob]:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(CronJobRow)
                .where(
                    CronJobRow.enabled,
                    CronJobRow.next_fire_at <= now,
                    claimable(stale_before),
                )
                .order_by(CronJobRow.next_fire_at)
                .limit(limit)
            )
            return [to_cron_job(row) for row in rows.all()]
