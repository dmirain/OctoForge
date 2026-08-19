"""SQL store of the collections module (dialect-neutral; the engine is not).

Quotas are enforced here by eviction, not refusal: collections are working
memory, and the least recently touched one is exactly what working memory
forgets first. The caller learns about an eviction from the counters, never
by an error — a fetch must not fail because an old fetch still lingers.
"""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionKind,
    CollectionNotFoundError,
    CollectionPassport,
    NewRecords,
)
from octoforge_core.net.collections.models import CollectionRecordRow, CollectionRow
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

    async def create(  # noqa: PLR0913, PLR0917 — a passport's worth of fields
        self,
        owner_id: str,
        label: str,
        kind: CollectionKind,
        source: str,
        schema: dict[str, Any],
        envelope: dict[str, Any],
        records: NewRecords,
        byte_size: int,
        truncated: bool,
        expires_at: datetime,
    ) -> CollectionPassport:
        """Create a collection with its first batch, evicting over-quota LRU."""
        async with write_session(self._session_factory) as session:
            await self._evict_for(session, owner_id, byte_size)
            row = CollectionRow(
                owner_id=owner_id,
                label=label,
                kind=kind.value,
                source=source,
                schema=schema,
                envelope=envelope,
                record_count=len(records.payloads),
                byte_size=byte_size,
                pages_loaded=1,
                truncated=truncated,
                expires_at=expires_at,
            )
            session.add(row)
            await session.flush()
            self._add_records(session, row.id, records, position=0)
            return _to_passport(row)

    async def append(  # noqa: PLR0913, PLR0917 — mirrors the port
        self,
        owner_id: str,
        collection_id: str,
        records: NewRecords,
        schema: dict[str, Any],
        byte_size: int,
        expires_at: datetime,
    ) -> CollectionPassport:
        """Append a batch, replace the derived schema, refresh the TTL."""
        async with write_session(self._session_factory) as session:
            row = await self._live_row(session, owner_id, collection_id)
            self._add_records(session, row.id, records, position=row.record_count)
            row.record_count += len(records.payloads)
            row.byte_size += byte_size
            row.pages_loaded += 1
            row.schema = schema
            row.expires_at = expires_at
            row.updated_at = utc_now()
            await session.flush()
            return _to_passport(row)

    async def passport(self, owner_id: str, collection_id: str) -> CollectionPassport:
        async with read_session(self._session_factory) as session:
            return _to_passport(await self._live_row(session, owner_id, collection_id))

    async def mark_truncated(self, owner_id: str, collection_id: str) -> None:
        """Persist that the source was cut mid-fill (page or wire limit)."""
        async with write_session(self._session_factory) as session:
            row = await self._live_row(session, owner_id, collection_id)
            row.truncated = True

    async def delete_expired(self) -> int:
        """Drop every collection past its TTL.

        Records are deleted explicitly rather than by cascade: SQLite does
        not enforce foreign keys unless a per-connection pragma says so, and
        a sweeper that silently strands records on one dialect is exactly the
        kind of divergence this module is not allowed to have.
        """
        now = utc_now()
        async with write_session(self._session_factory) as session:
            expired = select(CollectionRow.id).where(CollectionRow.expires_at <= now)
            ids = [row for row in (await session.scalars(expired)).all()]
            if not ids:
                return 0
            await session.execute(
                delete(CollectionRecordRow).where(CollectionRecordRow.collection_id.in_(ids))
            )
            await session.execute(delete(CollectionRow).where(CollectionRow.id.in_(ids)))
            return len(ids)

    def expiry_from_now(self) -> datetime:
        """Where the TTL puts a collection touched right now."""
        return utc_now() + timedelta(seconds=self._config.ttl_seconds)

    @staticmethod
    def _add_records(
        session: AsyncSession, collection_id: str, records: NewRecords, position: int
    ) -> None:
        session.add_all(
            CollectionRecordRow(
                collection_id=collection_id,
                source=records.source,
                position=position + index,
                payload=payload,
            )
            for index, payload in enumerate(records.payloads)
        )

    async def _live_row(
        self, session: AsyncSession, owner_id: str, collection_id: str
    ) -> CollectionRow:
        """The owner's un-expired collection row, or `CollectionNotFoundError`."""
        row = await session.get(CollectionRow, collection_id)
        if row is None or row.owner_id != owner_id or row.expires_at <= utc_now():
            raise CollectionNotFoundError(collection_id)
        return row

    async def _evict_for(self, session: AsyncSession, owner_id: str, incoming_bytes: int) -> None:
        """Make room under both quotas by dropping the least recently touched.

        Runs inside the caller's transaction: the eviction and the insert are
        one atomic step, so a crash between them cannot lose a quota's worth
        of collections for nothing.
        """
        rows = (
            await session.scalars(
                select(CollectionRow)
                .where(CollectionRow.owner_id == owner_id)
                .order_by(CollectionRow.updated_at, CollectionRow.id)
            )
        ).all()
        doomed: list[CollectionRow] = []
        survivors = list(rows)
        # count quota: the incoming collection is the +1
        while len(survivors) - len(doomed) >= self._config.max_per_user:
            doomed.append(survivors[len(doomed)])
        # byte quota: oldest keep going until the newcomer fits
        remaining = sum(row.byte_size for row in survivors[len(doomed) :])
        while doomed != survivors and remaining + incoming_bytes > self._config.max_bytes_per_user:
            candidate = survivors[len(doomed)]
            doomed.append(candidate)
            remaining -= candidate.byte_size
        if not doomed:
            return
        ids = [row.id for row in doomed]
        await session.execute(
            delete(CollectionRecordRow).where(CollectionRecordRow.collection_id.in_(ids))
        )
        await session.execute(delete(CollectionRow).where(CollectionRow.id.in_(ids)))

    @property
    def config(self) -> CollectionConfig:
        return self._config


def _to_passport(row: CollectionRow) -> CollectionPassport:
    return CollectionPassport(
        id=row.id,
        owner_id=row.owner_id,
        label=row.label,
        kind=CollectionKind(row.kind),
        source=row.source,
        schema=dict(row.schema),
        envelope=dict(row.envelope or {}),
        record_count=row.record_count,
        byte_size=row.byte_size,
        pages_loaded=row.pages_loaded,
        truncated=row.truncated,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )
