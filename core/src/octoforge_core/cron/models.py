"""ORM model of the cron module; the table is owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class CronJobRow(Base):
    """A user-owned scheduled job; claimed_* columns form the scheduler lease."""

    __tablename__ = "cron_jobs"
    __table_args__ = (Index("ix_cron_jobs_due", "enabled", "next_fire_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    schedule: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String)
    prompt: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean)
    next_fire_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_fire_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    one_shot: Mapped[bool] = mapped_column(Boolean, default=False)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
