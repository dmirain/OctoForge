"""Collection quota checks and least-recently-touched eviction."""

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from octoforge_core.net.collections.api import (
    CollectionAppend,
    CollectionConfig,
    CollectionQuotaError,
    NewCollection,
)
from octoforge_core.net.collections.models import CollectionRecordRow, CollectionRow


async def ensure_append_quota(
    session: AsyncSession, batch: CollectionAppend, config: CollectionConfig
) -> None:
    """Refuse growth that would pass the owner's total byte quota."""
    owned = await session.scalar(
        select(func.coalesce(func.sum(CollectionRow.byte_size), 0)).where(
            CollectionRow.owner_id == batch.owner_id
        )
    )
    if int(owned or 0) + batch.byte_size > config.max_bytes_per_user:
        raise CollectionQuotaError(
            f"appending {batch.byte_size} bytes would pass the "
            f"{config.max_bytes_per_user}-byte quota"
        )


async def evict_for(
    session: AsyncSession, collection: NewCollection, config: CollectionConfig
) -> None:
    """Drop the owner's LRU rows until both create quotas permit the new row."""
    rows = (
        await session.scalars(
            select(CollectionRow)
            .where(CollectionRow.owner_id == collection.owner_id)
            .order_by(CollectionRow.updated_at, CollectionRow.id)
        )
    ).all()
    doomed = _doomed_rows(list(rows), collection.byte_size, config)
    if doomed:
        await _delete_rows(session, [row.id for row in doomed])


def _doomed_rows(
    rows: list[CollectionRow], incoming_bytes: int, config: CollectionConfig
) -> list[CollectionRow]:
    doomed: list[CollectionRow] = []
    while len(rows) - len(doomed) >= config.max_per_user:
        doomed.append(rows[len(doomed)])
    remaining = sum(row.byte_size for row in rows[len(doomed) :])
    while doomed != rows and remaining + incoming_bytes > config.max_bytes_per_user:
        candidate = rows[len(doomed)]
        doomed.append(candidate)
        remaining -= candidate.byte_size
    return doomed


async def _delete_rows(session: AsyncSession, ids: list[str]) -> None:
    await session.execute(
        delete(CollectionRecordRow).where(CollectionRecordRow.collection_id.in_(ids))
    )
    await session.execute(delete(CollectionRow).where(CollectionRow.id.in_(ids)))
