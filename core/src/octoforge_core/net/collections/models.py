"""ORM models of the collections module; the two fixed tables live here.

`payload` and the schema/envelope documents are generic JSON with a JSONB
variant on Postgres: the query engine's `#>>` operators and casts want jsonb,
while `create_all` on SQLite still builds a valid (if unqueried) table — the
store is dialect-neutral, only the engine is not.

Record order is an explicit `position` column filled by the store from the
collection's own counter, not an autoincrement: a dialect-neutral way to keep
"the order the elements arrived in" without leaning on sequence semantics.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from octoforge_core.db.base import Base, UTCDateTime
from octoforge_core.time import utc_now

#: JSON everywhere, JSONB where the query engine runs.
PayloadJSON = JSON().with_variant(JSONB(), "postgresql")


class CollectionRow(Base):
    """One collection: its passport and the derived schema (a value, not DDL)."""

    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    owner_id: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String, default="")
    kind: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, default="")
    schema: Mapped[dict[str, Any]] = mapped_column(PayloadJSON)
    envelope: Mapped[dict[str, Any]] = mapped_column(PayloadJSON, default=dict)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    pages_loaded: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
    # the sweeper's index: expiry is scanned, never joined
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class CollectionRecordRow(Base):
    """One element of a collection's data."""

    __tablename__ = "collection_records"
    __table_args__ = (
        # every query scans one collection in element order
        Index("ix_collection_records_collection_position", "collection_id", "position"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    collection_id: Mapped[str] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    #: Which fetch the record came from (endpoint name; '' for a lone body).
    source: Mapped[str] = mapped_column(String, default="")
    #: Element order within the collection, dense from 0.
    position: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(PayloadJSON)
