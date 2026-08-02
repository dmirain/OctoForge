"""ORM model of the tasks module; the table is owned by this module."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.tasks.api import TaskStatus
from octoforge_core.time import utc_now


class TaskRow(Base):
    """A background task belonging to a dialog and a user."""

    __tablename__ = "tasks"
    __table_args__ = (
        # `list_undelivered` runs at startup over a table whose DONE branch
        # grows without bound by design; the partial index keeps that sweep
        # proportional to what is pending (migration f3b8d2c5a714). The
        # predicate is spelled per dialect or SQLite builds a full index.
        Index(
            "ix_tasks_undelivered",
            "status",
            sqlite_where=text("delivered_at IS NULL"),
            postgresql_where=text("delivered_at IS NULL"),
        ),
        # the two questions asked per turn, each filtering on both columns:
        # "what is this exchange's work" and "what did this dialog strand"
        Index("ix_tasks_exchange_status", "exchange_id", "status"),
        Index("ix_tasks_dialog_status", "dialog_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    # identity lives on the dialog, reached through this key — same as every
    # other table. Copies of user_id/channel used to ride along "for the
    # sweeps", but nothing ever filtered by them; the user_id copy even kept
    # an index no query read, maintained on the answer path's INSERT.
    dialog_id: Mapped[str] = mapped_column(ForeignKey("dialogs.id"), index=True)
    # the obligation this run is paying; NULL for RUN tasks (cron and spawned
    # work owe the user nothing) and on rows written before the column existed
    exchange_id: Mapped[str | None] = mapped_column(ForeignKey("exchanges.id"), nullable=True)
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
