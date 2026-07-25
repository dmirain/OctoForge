"""ORM model of the instructions module; the table is owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class InstructionRow(Base):
    """A knowledge/skill/endpoint record with its search embedding and stats.

    `owner_id` NULL marks a public record (visible to everyone); a non-NULL
    value is the owning user id (the record is visible to that user only).
    Private uniqueness is per (type, title, owner_id); public uniqueness per
    (type, title) is enforced by the partial index (NULLs are distinct in a
    plain unique constraint, so it cannot).
    """

    __tablename__ = "instructions"
    __table_args__ = (
        UniqueConstraint("type", "title", "owner_id"),
        # a unique constraint cannot guard NULL owners (NULLs never compare
        # equal), so public (type, title) pairs get a partial unique index
        Index(
            "uq_instructions_public_type_title",
            "type",
            "title",
            unique=True,
            # the predicate is required on every dialect: a plain unique index
            # over (type, title) would stop a private copy from shadowing the
            # public record
            sqlite_where=text("owner_id IS NULL"),
            postgresql_where=text("owner_id IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    type: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, index=True)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(JSON)
    tags: Mapped[list[str]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    system: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_id: Mapped[str | None] = mapped_column(String, index=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
