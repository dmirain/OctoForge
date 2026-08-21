"""SQL-backed collection persistence and its transaction boundaries."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.net.collections.api import (
    CollectionAppend,
    CollectionConfig,
    CollectionNotFoundError,
    CollectionPassport,
    NewCollection,
)
from octoforge_core.net.collections.models import CollectionRecordRow
from octoforge_core.net.collections.store_expiry import delete_expired
from octoforge_core.net.collections.store_quota import ensure_append_quota, evict_for
from octoforge_core.net.collections.store_records import (
    add_appended_records,
    add_initial_records,
    live_row,
    lock_collection,
    new_row,
    to_passport,
)
from octoforge_core.time import utc_now


class SqlAlchemyCollectionStore:
    """Rows of the two fixed tables; owner scoping is a SQL predicate."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: CollectionConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or CollectionConfig()

    async def create(self, collection: NewCollection) -> CollectionPassport:
        """Create a collection with its first batch, evicting over-quota LRU."""
        async with write_session(self._session_factory) as session:
            await evict_for(session, collection, self._config)
            row = new_row(collection)
            session.add(row)
            await session.flush()
            add_initial_records(session, row, collection.records)
            return to_passport(row)

    async def append(self, batch: CollectionAppend) -> CollectionPassport:
        """Append a batch, replace its schema, and refresh its TTL."""
        async with write_session(self._session_factory) as session:
            await lock_collection(session, batch.collection_id)
            await ensure_append_quota(session, batch, self._config)
            row = await live_row(session, batch.owner_id, batch.collection_id)
            add_appended_records(session, row, batch.records)
            row.record_count += len(batch.records.payloads)
            row.byte_size += batch.byte_size
            row.pages_loaded += 1
            row.schema = batch.schema
            row.expires_at = batch.expires_at
            row.updated_at = utc_now()
            await session.flush()
            return to_passport(row)

    async def passport(self, owner_id: str, collection_id: str) -> CollectionPassport:
        async with read_session(self._session_factory) as session:
            return to_passport(await live_row(session, owner_id, collection_id))

    async def mark_truncated(self, owner_id: str, collection_id: str) -> None:
        """Persist that the source was cut mid-fill (page or wire limit)."""
        async with write_session(self._session_factory) as session:
            row = await live_row(session, owner_id, collection_id)
            row.truncated = True

    async def single_payload(self, owner_id: str, collection_id: str) -> dict[str, Any]:
        """Return the first record's payload for a parked document."""
        async with read_session(self._session_factory) as session:
            await live_row(session, owner_id, collection_id)
            payload = await session.scalar(
                select(CollectionRecordRow.payload)
                .where(CollectionRecordRow.collection_id == collection_id)
                .order_by(CollectionRecordRow.position)
                .limit(1)
            )
            if payload is None:
                raise CollectionNotFoundError(collection_id)
            return dict(payload)

    async def delete_expired(self) -> int:
        now = utc_now()
        async with write_session(self._session_factory) as session:
            return await delete_expired(session, now)

    def expiry_from_now(self) -> datetime:
        """Return the expiry of a collection touched right now."""
        return utc_now() + timedelta(seconds=self._config.ttl_seconds)

    @property
    def config(self) -> CollectionConfig:
        return self._config
