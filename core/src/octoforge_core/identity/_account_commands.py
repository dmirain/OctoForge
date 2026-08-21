"""Transactional ownership changes for surface accounts."""

import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import write_session
from octoforge_core.identity._account_queries import find_identity_row, to_identity
from octoforge_core.identity.models import UserIdentityRow
from octoforge_core.identity.types import (
    IdentityKey,
    IdentityLink,
    IdentityNotFoundError,
    IdentityTakenError,
    UserIdentity,
)
from octoforge_core.time import utc_now


class IdentityAccountCommands:
    """Enforce exclusive ownership while accounts are linked, moved, or revoked."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def link(self, request: IdentityLink) -> UserIdentity:
        """Attach an account, reviving it only for the person who owned it."""
        async with self._session_factory() as session:
            existing = await find_identity_row(session, request.key)
            if existing is not None:
                if existing.user_id != request.user_id:
                    raise IdentityTakenError(f"{request.key.surface}:{request.key.external_id}")
                existing.active = True
                if request.details is not None:
                    existing.details = request.details
                await session.commit()
                return to_identity(existing)
            row = self._new_row(request)
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as clash:
                await session.rollback()
                raise IdentityTakenError(
                    f"{request.key.surface}:{request.key.external_id}"
                ) from clash
            return to_identity(row)

    async def reseat(self, surface: str, user_id: str, external_id: str) -> UserIdentity:
        """Move a person's identity to an unclaimed account on one surface."""
        destination = IdentityKey(surface, external_id)
        async with write_session(self._session_factory) as session:
            taken = await find_identity_row(session, destination)
            if taken is not None and taken.user_id != user_id:
                raise IdentityTakenError(f"{surface}:{external_id}")
            rows = await session.scalars(
                select(UserIdentityRow).where(
                    UserIdentityRow.surface == surface,
                    UserIdentityRow.user_id == user_id,
                )
            )
            row = rows.first()
            if row is None:
                raise IdentityNotFoundError(f"{surface}:{user_id}")
            row.external_id = external_id
            row.active = True
            row.updated_at = utc_now()
            return to_identity(row)

    async def deactivate(self, key: IdentityKey) -> None:
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(UserIdentityRow)
                .where(
                    UserIdentityRow.surface == key.surface,
                    UserIdentityRow.external_id == key.external_id,
                )
                .values(active=False, updated_at=utc_now())
            )

    @staticmethod
    def _new_row(request: IdentityLink) -> UserIdentityRow:
        return UserIdentityRow(
            id=uuid.uuid4().hex,
            user_id=request.user_id,
            surface=request.key.surface,
            external_id=request.key.external_id,
            details=request.details or {},
        )
