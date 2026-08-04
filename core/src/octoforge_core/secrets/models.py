"""ORM models of the secrets module; the tables are owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class SecretRow(Base):
    """One encrypted per-user secret, bound to the only host it may be sent to."""

    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("user_id", "code"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String, index=True)
    code: Mapped[str] = mapped_column(String)
    ciphertext: Mapped[str] = mapped_column(String)
    allowed_host: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    # comma-joined SecretPlacement values; NULL = the header-only default
    placements: Mapped[str | None] = mapped_column(String, default=None)
    # a SecretTransform value; NULL = substitute the value as stored
    transform: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class SecretFormLinkRow(Base):
    """One short-lived capability code for the secrets form.

    The stateless Fernet token it replaces was ~700 characters, and the agent
    had to transcribe it into chat verbatim — which is exactly what a model
    does badly: on 2026-08-04 it twice invented a plausible-looking token
    instead of copying the real one, once looping for 30 000 characters. A
    short code is copyable; the payload stays here.
    """

    __tablename__ = "secret_form_links"
    __table_args__ = (Index("ix_secret_form_links_expires_at", "expires_at"),)

    code: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    # the form prefill as JSON; NULL for a link that opens an empty form
    prefill: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
