"""SQLAlchemy storage of the cron module (module-internal).

Follows the `memory/store.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to DTOs at the boundary. The lease
operations (`claim`, `complete_fire`, `release_claim`) are single UPDATE
statements so concurrent scheduler instances stay exactly-once.
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from octoforge_core.cron.api import CronJob, CronJobNotFoundError, CronStore
from octoforge_core.cron.models import CronJobRow
from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.tasks.api import TaskStatus


class SqlAlchemyCronStore(CronStore):
    """SQL persistence for cron jobs, including the lease CAS."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, job: CronJob) -> CronJob:
        """Persist a fully populated job; return the stored copy."""
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
            return _to_cron_job(row)

    async def get(self, job_id: str) -> CronJob:
        """Return the job by id regardless of owner; raise `CronJobNotFoundError`."""
        async with read_session(self._session_factory) as session:
            row = await session.get(CronJobRow, job_id)
            if row is None:
                raise CronJobNotFoundError(f"cron job '{job_id}' not found")
            return _to_cron_job(row)

    async def list_for_user(self, user_id: str) -> list[CronJob]:
        """Return all jobs of this owner, oldest first."""
        async with read_session(self._session_factory) as session:
            statement = (
                select(CronJobRow)
                .where(CronJobRow.user_id == user_id)
                .order_by(CronJobRow.created_at, CronJobRow.id)
            )
            rows = (await session.scalars(statement)).all()
            return [_to_cron_job(row) for row in rows]

    async def delete_for_user(self, user_id: str, job_id: str) -> None:
        """Delete the owner's job; raise `CronJobNotFoundError` for foreign/missing ids."""
        async with write_session(self._session_factory) as session:
            row = await _find_owned_row(session, user_id, job_id)
            await session.delete(row)

    async def set_enabled(
        self,
        user_id: str,
        job_id: str,
        enabled: bool,
        next_fire_at: datetime | None = None,
    ) -> CronJob:
        """Pause/resume the owner's job, optionally moving `next_fire_at`."""
        async with write_session(self._session_factory) as session:
            row = await _find_owned_row(session, user_id, job_id)
            row.enabled = enabled
            if next_fire_at is not None:
                row.next_fire_at = next_fire_at
            return _to_cron_job(row)

    async def list_due(self, now: datetime, stale_before: datetime, limit: int) -> list[CronJob]:
        """Return enabled claimable jobs due at `now`, earliest first."""
        async with read_session(self._session_factory) as session:
            statement = (
                select(CronJobRow)
                .where(
                    CronJobRow.enabled,
                    CronJobRow.next_fire_at <= now,
                    _claimable_clause(stale_before),
                )
                .order_by(CronJobRow.next_fire_at)
                .limit(limit)
            )
            rows = (await session.scalars(statement)).all()
            return [_to_cron_job(row) for row in rows]

    async def claim(
        self,
        job_id: str,
        expected_next_fire_at: datetime,
        owner: str,
        now: datetime,
        stale_before: datetime,
    ) -> bool:
        """Take the lease with one conditional UPDATE; False means the race was lost."""
        async with write_session(self._session_factory) as session:
            statement = (
                update(CronJobRow)
                .where(
                    CronJobRow.id == job_id,
                    # a pause racing the claim after list_due must win
                    CronJobRow.enabled,
                    CronJobRow.next_fire_at == expected_next_fire_at,
                    _claimable_clause(stale_before),
                )
                .values(claimed_by=owner, claimed_at=now)
            )
            # DML executes into a CursorResult at runtime; the stubs type
            # AsyncSession.execute loosely, so narrow it for rowcount.
            result = cast(CursorResult[Any], await session.execute(statement))
            return result.rowcount > 0

    async def release_claim(self, job_id: str) -> None:
        """Drop the lease without firing (the wake delivery failed)."""
        async with write_session(self._session_factory) as session:
            statement = (
                update(CronJobRow)
                .where(CronJobRow.id == job_id)
                .values(claimed_by=None, claimed_at=None)
            )
            await session.execute(statement)

    async def complete_fire(self, job_id: str, fired_at: datetime, next_fire_at: datetime) -> None:
        """Record a successful fire: bump last/next fire times and drop the lease."""
        async with write_session(self._session_factory) as session:
            statement = (
                update(CronJobRow)
                .where(CronJobRow.id == job_id)
                .values(
                    last_fire_at=fired_at,
                    next_fire_at=next_fire_at,
                    claimed_by=None,
                    claimed_at=None,
                )
            )
            await session.execute(statement)

    async def record_fire_result(
        self,
        job_id: str,
        status: TaskStatus,
        error: str | None,
        retry_at: datetime | None,
    ) -> None:
        """Apply the fired process outcome and the retry decision in one UPDATE."""
        async with write_session(self._session_factory) as session:
            if retry_at is not None:
                statement = (
                    update(CronJobRow)
                    .where(CronJobRow.id == job_id)
                    .values(
                        last_status=status.value,
                        last_error=error,
                        next_fire_at=retry_at,
                        retry_count=CronJobRow.retry_count + 1,
                    )
                )
            else:
                statement = (
                    update(CronJobRow)
                    .where(CronJobRow.id == job_id)
                    .values(
                        last_status=status.value,
                        last_error=error,
                        retry_count=0,
                    )
                )
            await session.execute(statement)


def _claimable_clause(stale_before: datetime) -> ColumnElement[bool]:
    return or_(
        CronJobRow.claimed_by.is_(None),
        CronJobRow.claimed_at < stale_before,
    )


async def _find_owned_row(
    session: AsyncSession,
    user_id: str,
    job_id: str,
) -> CronJobRow:
    statement = select(CronJobRow).where(
        CronJobRow.id == job_id,
        CronJobRow.user_id == user_id,
    )
    row = (await session.scalars(statement)).first()
    if row is None:
        raise CronJobNotFoundError(f"cron job '{job_id}' not found for user '{user_id}'")
    return row


def _to_cron_job(row: CronJobRow) -> CronJob:
    return CronJob(
        id=row.id,
        user_id=row.user_id,
        channel=row.channel,
        title=row.title,
        schedule=row.schedule,
        timezone=row.timezone,
        prompt=row.prompt,
        enabled=row.enabled,
        next_fire_at=row.next_fire_at,
        last_fire_at=row.last_fire_at,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        created_at=row.created_at,
        one_shot=row.one_shot,
        last_status=None if row.last_status is None else TaskStatus(row.last_status),
        last_error=row.last_error,
        retry_count=row.retry_count,
    )
