"""Collection-row construction, record insertion, and ownership reads."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from octoforge_core.net.collections.api import (
    CollectionKind,
    CollectionNotFoundError,
    CollectionPassport,
    NewCollection,
    NewRecords,
)
from octoforge_core.net.collections.models import CollectionRecordRow, CollectionRow
from octoforge_core.time import utc_now


def new_row(collection: NewCollection) -> CollectionRow:
    """Build the collection row for a create command."""
    return CollectionRow(
        owner_id=collection.owner_id,
        label=collection.label,
        kind=collection.kind.value,
        source=collection.source,
        schema=collection.schema,
        envelope=collection.envelope,
        record_count=len(collection.records.payloads),
        byte_size=collection.byte_size,
        pages_loaded=1,
        truncated=collection.truncated,
        expires_at=collection.expires_at,
    )


def add_initial_records(session: AsyncSession, row: CollectionRow, records: NewRecords) -> None:
    """Stage the first batch at dense positions starting from zero."""
    session.add_all(_record_rows(row.id, records, 0))


def add_appended_records(session: AsyncSession, row: CollectionRow, records: NewRecords) -> None:
    """Stage an appended batch after the collection's current tail."""
    session.add_all(_record_rows(row.id, records, row.record_count))


def _record_rows(
    collection_id: str, records: NewRecords, position: int
) -> list[CollectionRecordRow]:
    return [
        CollectionRecordRow(
            collection_id=collection_id,
            source=records.source,
            position=position + index,
            payload=payload,
        )
        for index, payload in enumerate(records.payloads)
    ]


async def lock_collection(session: AsyncSession, collection_id: str) -> None:
    """Serialize one collection's writers on Postgres; do nothing elsewhere."""
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))").bindparams(
                key=f"collection:{collection_id}"
            )
        )


async def live_row(session: AsyncSession, owner_id: str, collection_id: str) -> CollectionRow:
    """Return the owner's unexpired row or raise the stable not-found error."""
    row = await session.get(CollectionRow, collection_id)
    if row is None or row.owner_id != owner_id or row.expires_at <= utc_now():
        raise CollectionNotFoundError(collection_id)
    return row


def to_passport(row: CollectionRow) -> CollectionPassport:
    """Map a persisted row to the public collection passport."""
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
