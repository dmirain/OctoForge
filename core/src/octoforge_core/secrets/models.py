"""ORM model of the secrets module; the table is owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import String, UniqueConstraint
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
