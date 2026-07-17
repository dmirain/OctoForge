"""Declarative base and the UTC-enforcing datetime column type."""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class UTCDateTime(TypeDecorator[datetime]):
    """DateTime coerced to timezone-aware UTC on write and on read.

    SQLite stores datetimes without timezone information, so values are
    normalized to aware UTC both when bound and when read back.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)
