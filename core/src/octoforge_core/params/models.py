"""ORM model of the user-params module; the table is owned by this module."""

import uuid
from datetime import datetime

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now


class UserParamRow(Base):
    """One per-user non-secret parameter, plaintext by contract."""

    __tablename__ = "user_params"
    __table_args__ = (UniqueConstraint("user_id", "code"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[str] = mapped_column(String, index=True)
    code: Mapped[str] = mapped_column(String)
    value: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
