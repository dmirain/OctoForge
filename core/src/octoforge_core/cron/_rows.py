"""Cron SQL row lookup, lease predicates, and DTO mapping."""

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from octoforge_core.cron.models import CronJobRow
from octoforge_core.cron.types import CronJob, CronJobNotFoundError
from octoforge_core.tasks.api import TaskStatus


def claimable(stale_before: datetime) -> ColumnElement[bool]:
    return or_(CronJobRow.claimed_by.is_(None), CronJobRow.claimed_at < stale_before)


async def find_owned(session: AsyncSession, user_id: str, job_id: str) -> CronJobRow:
    row = (
        await session.scalars(
            select(CronJobRow).where(
                CronJobRow.id == job_id,
                CronJobRow.user_id == user_id,
            )
        )
    ).first()
    if row is None:
        raise CronJobNotFoundError(f"cron job '{job_id}' not found for user '{user_id}'")
    return row


def to_cron_job(row: CronJobRow) -> CronJob:
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
