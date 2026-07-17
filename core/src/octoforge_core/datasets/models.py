"""ORM models of the datasets module; the tables are owned by this module."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class DatasetRow(Base):
    """A user-owned dataset descriptor with its search embedding."""

    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("owner_user_id", "name"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_user_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    # Raw JSON schema document: {"fields": [{"name", "type", "required"}]}.
    schema: Mapped[dict[str, Any]] = mapped_column(JSON)
    usage_notes: Mapped[str] = mapped_column(String, default="")
    retention: Mapped[str] = mapped_column(String, default="")
    embedding: Mapped[list[float]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class DatasetRecordRow(Base):
    """One JSON record of a dataset, owned by the dataset's owner."""

    __tablename__ = "dataset_records"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
