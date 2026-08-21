"""Identity account lookup and read projections."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session
from octoforge_core.identity.models import UserIdentityRow
from octoforge_core.identity.types import IdentityKey, UserIdentity, UserIdentityList


class IdentityAccountQueries:
    """Read surface accounts with stable ordering and DTO mapping."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, key: IdentityKey) -> str | None:
        async with read_session(self._session_factory) as session:
            user_id: str | None = await session.scalar(
                select(UserIdentityRow.user_id).where(
                    UserIdentityRow.surface == key.surface,
                    UserIdentityRow.external_id == key.external_id,
                    UserIdentityRow.active.is_(True),
                )
            )
            return user_id

    async def of_user(self, user_id: str) -> UserIdentityList:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(UserIdentityRow)
                .where(UserIdentityRow.user_id == user_id)
                .order_by(UserIdentityRow.surface, UserIdentityRow.created_at)
            )
            return [to_identity(row) for row in rows.all()]

    async def list(self, surface: str) -> UserIdentityList:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(UserIdentityRow)
                .where(UserIdentityRow.surface == surface)
                .order_by(UserIdentityRow.created_at, UserIdentityRow.id)
            )
            return [to_identity(row) for row in rows.all()]

    async def find(self, key: IdentityKey) -> UserIdentity | None:
        async with read_session(self._session_factory) as session:
            row = await find_identity_row(session, key)
            return to_identity(row) if row is not None else None


async def find_identity_row(session: AsyncSession, key: IdentityKey) -> UserIdentityRow | None:
    rows = await session.scalars(
        select(UserIdentityRow).where(
            UserIdentityRow.surface == key.surface,
            UserIdentityRow.external_id == key.external_id,
        )
    )
    return rows.first()


def to_identity(row: UserIdentityRow) -> UserIdentity:
    return UserIdentity(
        user_id=row.user_id,
        surface=row.surface,
        external_id=row.external_id,
        name=row.name,
        username=row.username,
        details=dict(row.details or {}),
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
