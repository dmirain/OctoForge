"""ORM model of the context module; the table is owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class SummaryRow(Base):
    """A compressed segment of a dialog's message archive, tagged with topics."""

    __tablename__ = "dialog_summaries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    dialog_id: Mapped[str] = mapped_column(ForeignKey("dialogs.id"), index=True)
    seq_from: Mapped[int] = mapped_column(Integer)
    seq_to: Mapped[int] = mapped_column(Integer)
    topics: Mapped[list[str]] = mapped_column(JSON)
    content: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
