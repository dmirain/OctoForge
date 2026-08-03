"""ORM models of the identity module."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class UserRow(Base):
    """A person. The id is opaque on purpose — see `identity/api.py`."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    # the person's canonical name: seeded from the first surface that reports
    # one, thereafter the person's own ("" only until any surface has)
    name: Mapped[str] = mapped_column(String, default="")
    # The cross-cutting identifier once registration exists. Unique so two
    # people cannot claim one address, nullable because nobody has one yet —
    # "" would collide on the second row.
    email: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class UserIdentityRow(Base):
    """What one surface calls a user.

    The unique constraint is the reason this is a table rather than a JSON
    column on `users`: an account belonging to two people is the one thing
    that must be impossible, and only the database can promise it.
    """

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("surface", "external_id", name="uq_identity_account"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    # indexed: "who is this" runs on every incoming message
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    surface: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    # living mirror of the surface profile: the display name the surface
    # currently shows for them ("" until the surface reports one) and the
    # optional handle — a username is a surface luxury, hence nullable
    name: Mapped[str] = mapped_column(String, default="")
    username: Mapped[str | None] = mapped_column(String, nullable=True, default=None)
    # surface-specific extras; nothing here needs enforcing, which is exactly
    # why JSON is right here and wrong for the identity itself
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
