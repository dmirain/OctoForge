"""Expired collection deletion shared by the SQL store."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from octoforge_core.net.collections.models import CollectionRecordRow, CollectionRow


async def delete_expired(session: AsyncSession, now: datetime) -> int:
    """Delete expired collections and their records explicitly on every dialect."""
    expired = select(CollectionRow.id).where(CollectionRow.expires_at <= now)
    ids = list((await session.scalars(expired)).all())
    if not ids:
        return 0
    await session.execute(
        delete(CollectionRecordRow).where(CollectionRecordRow.collection_id.in_(ids))
    )
    await session.execute(delete(CollectionRow).where(CollectionRow.id.in_(ids)))
    return len(ids)
