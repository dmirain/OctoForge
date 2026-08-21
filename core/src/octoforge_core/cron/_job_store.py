"""Cron job creation, owner deletion, and enablement changes."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.cron._rows import find_owned, to_cron_job
from octoforge_core.cron.models import CronJobRow
from octoforge_core.cron.types import CronEnablement, CronJob
from octoforge_core.db.unit_of_work import write_session


class CronJobStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, job: CronJob) -> CronJob:
        async with write_session(self._session_factory) as session:
            row = CronJobRow(
                id=job.id,
                user_id=job.user_id,
                channel=job.channel,
                title=job.title,
                schedule=job.schedule,
                timezone=job.timezone,
                prompt=job.prompt,
                enabled=job.enabled,
                next_fire_at=job.next_fire_at,
                last_fire_at=job.last_fire_at,
                claimed_by=job.claimed_by,
                claimed_at=job.claimed_at,
                created_at=job.created_at,
                one_shot=job.one_shot,
                last_status=None if job.last_status is None else job.last_status.value,
                last_error=job.last_error,
                retry_count=job.retry_count,
            )
            session.add(row)
            return to_cron_job(row)

    async def delete_for_user(self, user_id: str, job_id: str) -> None:
        async with write_session(self._session_factory) as session:
            await session.delete(await find_owned(session, user_id, job_id))

    async def set_enabled(self, request: CronEnablement) -> CronJob:
        async with write_session(self._session_factory) as session:
            row = await find_owned(session, request.user_id, request.job_id)
            row.enabled = request.enabled
            if request.next_fire_at is not None:
                row.next_fire_at = request.next_fire_at
            return to_cron_job(row)
