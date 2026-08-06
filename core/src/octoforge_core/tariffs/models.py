"""ORM models of the tariffs module; the tables are owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class TariffRow(Base):
    """One operator-defined plan; NULL limits mean unlimited."""

    __tablename__ = "tariffs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    code: Mapped[str] = mapped_column(String, unique=True)
    title: Mapped[str] = mapped_column(String)
    # A JSON list of FeatureCode values: the set grows without migrations.
    features: Mapped[list[str]] = mapped_column(JSON, default=list)
    daily_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    daily_user_messages: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    daily_assistant_messages: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    max_cron_jobs: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    max_datasets: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    max_memory_chars: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # at most one row is true; the store keeps the invariant on every put
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class UserTariffRow(Base):
    """The user's plan binding; absence of a row means unlimited."""

    __tablename__ = "user_tariffs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String, index=True, unique=True)
    tariff_id: Mapped[str] = mapped_column(String)
    assigned_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class UsageEventRow(Base):
    """One metered action, append-only; reports and limit checks sum over it."""

    __tablename__ = "usage_events"
    __table_args__ = (Index("ix_usage_events_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    origin: Mapped[str] = mapped_column(String)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    dialog_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    exchange_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True, default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
