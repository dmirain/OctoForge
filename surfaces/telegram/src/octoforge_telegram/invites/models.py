"""ORM models of the invite store: the Telegram surface's own database."""

from datetime import datetime

from octoforge_core.db.base import UTCDateTime
from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_telegram.schema import TelegramSurfaceBase


class MemberRow(TelegramSurfaceBase):
    """Profile of a Telegram user who passed the gate, refreshed on contact.

    Names and usernames change, so this is a living mirror of what Telegram
    sent last, not an immutable record; `first_seen_at` is the entry moment.
    """

    __tablename__ = "members"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    first_name: Mapped[str] = mapped_column(String, default="")
    last_name: Mapped[str] = mapped_column(String, default="")
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)


class InviteRow(TelegramSurfaceBase):
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
