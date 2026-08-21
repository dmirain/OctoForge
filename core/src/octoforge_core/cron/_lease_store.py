"""Atomic cron lease acquisition, completion, and process outcomes."""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.cron._rows import claimable
from octoforge_core.cron.models import CronJobRow
from octoforge_core.cron.types import CronClaim, CronFireResult
from octoforge_core.db.unit_of_work import write_session


class CronLeaseStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(self, request: CronClaim) -> bool:
        async with write_session(self._session_factory) as session:
            statement = (
                update(CronJobRow)
                .where(
                    CronJobRow.id == request.job_id,
                    CronJobRow.enabled,
                    CronJobRow.next_fire_at == request.expected_next_fire_at,
                    claimable(request.stale_before),
                )
                .values(claimed_by=request.owner, claimed_at=request.now)
            )
            result = cast(CursorResult[Any], await session.execute(statement))
            return result.rowcount > 0

    async def release(self, job_id: str) -> None:
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(CronJobRow)
                .where(CronJobRow.id == job_id)
                .values(claimed_by=None, claimed_at=None)
            )

    async def complete(self, job_id: str, fired_at: datetime, next_fire_at: datetime) -> None:
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(CronJobRow)
                .where(CronJobRow.id == job_id)
                .values(
                    last_fire_at=fired_at,
                    next_fire_at=next_fire_at,
                    claimed_by=None,
                    claimed_at=None,
                )
            )

    async def record_result(self, result: CronFireResult) -> None:
        values: dict[str, object] = {
            "last_status": result.status.value,
            "last_error": result.error,
            "retry_count": 0,
        }
        if result.retry_at is not None:
            values["next_fire_at"] = result.retry_at
            values["retry_count"] = CronJobRow.retry_count + 1
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(CronJobRow).where(CronJobRow.id == result.job_id).values(**values)
            )
