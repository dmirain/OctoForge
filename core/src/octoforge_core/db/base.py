"""Declarative base and the UTC-enforcing datetime column type."""

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator, TypeEngine

POSTGRESQL_DIALECT = "postgresql"


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class UTCDateTime(TypeDecorator[datetime]):
    """DateTime coerced to timezone-aware UTC on write and on read.

    The Python-side contract is the same on every dialect — aware UTC in, aware
    UTC out — but the column type is not. Postgres gets a native `timestamptz`
    and keeps the offset itself (asyncpg rejects an aware value bound to a
    `timestamp without time zone`). SQLite has no timezone support at all, so
    values are stored naive after normalization to UTC, which is byte-identical
    to what earlier versions wrote, and re-stamped as UTC when read back.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[datetime]:
        """Use a timezone-aware column on Postgres, a naive one elsewhere."""
        if dialect.name == POSTGRESQL_DIALECT:
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if dialect.name == POSTGRESQL_DIALECT:
            return moment
        return moment.replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
