"""ORM model of the invite store: own Base, own database, no Alembic.

The separate DeclarativeBase keeps the invites table out of core's
`Base.metadata`, so it cannot leak into any core tooling even by accident.
"""

from datetime import datetime

from octoforge_core.db.base import UTCDateTime
from sqlalchemy import JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class InviteBase(DeclarativeBase):
    """Base class of the Telegram invite schema (isolated from core's Base)."""


class InviteRow(InviteBase):
    """One invite code: pending -> claimed -> (revoked -> claimed)."""

    __tablename__ = "invites"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[str] = mapped_column(String)
    note: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    disabled_cron_job_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
