"""ORM model of the memory module; the table is owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class MemoryRow(Base):
    """A user-owned memory entry; a NULL user_id marks a global entry."""

    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("user_id", "key"),
        # a unique constraint cannot guard NULL owners (NULLs never compare
        # equal), so global keys get a partial unique index instead
        Index(
            "uq_memories_global_key",
            "key",
            unique=True,
            # every dialect that runs this schema needs the predicate: without
            # it the index is a plain unique one over `key`, which would forbid
            # two users from holding the same key
            sqlite_where=text("user_id IS NULL"),
            postgresql_where=text("user_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str | None] = mapped_column(String, index=True)
    key: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(String)
    tags: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
