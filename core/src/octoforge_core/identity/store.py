"""SQL store of the identity module."""

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.identity.api import (
    IdentityNotFoundError,
    IdentityTakenError,
    User,
    UserIdentity,
    UserIdentityList,
    UserList,
    UserNotFoundError,
)
from octoforge_core.identity.models import UserIdentityRow, UserRow
from octoforge_core.time import utc_now


class SqlAlchemyIdentityStore:
    """Users and the surface identities pointing at them."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(self, surface: str, external_id: str) -> str | None:
        """The user this account belongs to, or None when nobody active claims it."""
        async with read_session(self._session_factory) as session:
            user_id: str | None = await session.scalar(
                select(UserIdentityRow.user_id).where(
                    UserIdentityRow.surface == surface,
                    UserIdentityRow.external_id == external_id,
                    UserIdentityRow.active.is_(True),
                )
            )
            return user_id

    async def resolve_or_create(self, surface: str, external_id: str) -> str:
        """The user this account belongs to, minting one on first contact.

        Two first messages arriving together must not mint two people: the
        loser of the unique constraint re-reads instead of raising, so both
        end up on the same person.
        """
        found = await self.resolve(surface, external_id)
        if found is not None:
            return found
        user = await self.create_user()
        try:
            await self.link(user.id, surface, external_id)
        except IdentityTakenError:
            owner = await self.resolve(surface, external_id)
            if owner is None:  # taken by a revoked identity: nobody may enter
                raise
            return owner
        return user.id

    async def create_user(self, email: str = "") -> User:
        """Mint a person with an id of their own."""
        row = UserRow(id=uuid.uuid4().hex, email=email or None)
        async with write_session(self._session_factory) as session:
            session.add(row)
            await session.flush()
            return _to_user(row)

    async def get_user(self, user_id: str) -> User:
        async with read_session(self._session_factory) as session:
            row = await session.get(UserRow, user_id)
            if row is None:
                raise UserNotFoundError(user_id)
            return _to_user(row)

    async def link(
        self, user_id: str, surface: str, external_id: str, details: dict[str, Any] | None = None
    ) -> UserIdentity:
        """Attach an account to a person; raise if it is already somebody's.

        A previously revoked identity of the SAME person is revived rather
        than refused: coming back is not a conflict.

        Deliberately NOT unit-of-work aware (raw `_session_factory`): the
        insert branch uses the commit itself as the uniqueness-race detector
        and rolls the session back on the clash, which inside a shared
        transaction would discard the unit's earlier writes.
        """
        async with self._session_factory() as session:
            existing = await self._find(session, surface, external_id)
            if existing is not None:
                if existing.user_id != user_id:
                    raise IdentityTakenError(f"{surface}:{external_id}")
                existing.active = True
                if details is not None:
                    existing.details = details
                await session.commit()
                return _to_identity(existing)
            row = UserIdentityRow(
                id=uuid.uuid4().hex,
                user_id=user_id,
                surface=surface,
                external_id=external_id,
                details=details or {},
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as clash:
                # somebody claimed it between the check and the insert
                await session.rollback()
                raise IdentityTakenError(f"{surface}:{external_id}") from clash
            return _to_identity(row)

    async def reseat(self, surface: str, user_id: str, external_id: str) -> UserIdentity:
        """Point this person's identity on a surface at a different account."""
        async with write_session(self._session_factory) as session:
            taken = await self._find(session, surface, external_id)
            if taken is not None and taken.user_id != user_id:
                raise IdentityTakenError(f"{surface}:{external_id}")
            rows = await session.scalars(
                select(UserIdentityRow).where(
                    UserIdentityRow.surface == surface, UserIdentityRow.user_id == user_id
                )
            )
            row = rows.first()
            if row is None:
                raise IdentityNotFoundError(f"{surface}:{user_id}")
            row.external_id = external_id
            row.active = True
            row.updated_at = utc_now()
            return _to_identity(row)

    async def deactivate(self, surface: str, external_id: str) -> None:
        """Revoke an account without erasing that it was once used."""
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(UserIdentityRow)
                .where(
                    UserIdentityRow.surface == surface,
                    UserIdentityRow.external_id == external_id,
                )
                .values(active=False, updated_at=utc_now())
            )

    async def list_users(self) -> UserList:
        """Everyone the installation knows, newest first."""
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(select(UserRow).order_by(UserRow.created_at.desc()))
            return [_to_user(row) for row in rows.all()]

    async def identities_of(self, user_id: str) -> UserIdentityList:
        """Every surface this person is known on, revoked ones included."""
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(UserIdentityRow)
                .where(UserIdentityRow.user_id == user_id)
                .order_by(UserIdentityRow.surface, UserIdentityRow.created_at)
            )
            return [_to_identity(row) for row in rows.all()]

    async def find_by_identity(self, surface: str, external_id: str) -> UserIdentity | None:
        async with read_session(self._session_factory) as session:
            row = await self._find(session, surface, external_id)
            return _to_identity(row) if row is not None else None

    @staticmethod
    async def _find(
        session: AsyncSession, surface: str, external_id: str
    ) -> UserIdentityRow | None:
        rows = await session.scalars(
            select(UserIdentityRow).where(
                UserIdentityRow.surface == surface, UserIdentityRow.external_id == external_id
            )
        )
        return rows.first()


def _to_user(row: UserRow) -> User:
    return User(id=row.id, email=row.email or "", created_at=row.created_at)


def _to_identity(row: UserIdentityRow) -> UserIdentity:
    return UserIdentity(
        user_id=row.user_id,
        surface=row.surface,
        external_id=row.external_id,
        details=dict(row.details or {}),
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
