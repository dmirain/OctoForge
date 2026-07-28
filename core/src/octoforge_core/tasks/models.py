"""ORM model of the tasks module; the table is owned by this module."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.tasks.api import TaskStatus
from octoforge_core.time import utc_now


class TaskRow(Base):
    """A background task belonging to a dialog and a user."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    dialog_id: Mapped[str] = mapped_column(ForeignKey("dialogs.id"), index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    channel: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    # indexed: the startup sweeps (list_orphaned, list_undelivered) filter by
    # status over a table that never deletes rows
    status: Mapped[str] = mapped_column(String, default=TaskStatus.PENDING.value, index=True)
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
