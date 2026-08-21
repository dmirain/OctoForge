"""Person persistence and admission under the active-user cap."""

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.identity.models import UserRow
from octoforge_core.identity.types import User, UserList, UserNotFoundError, UserStatus


class UserStore:
    """Persist people and serialize capped activation where the dialect requires it."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(self, email: str = "") -> User:
        row = UserRow(id=uuid.uuid4().hex, email=email or None)
        async with write_session(self._session_factory) as session:
            session.add(row)
            await session.flush()
            return _to_user(row)

    async def get(self, user_id: str) -> User:
        async with read_session(self._session_factory) as session:
            row = await session.get(UserRow, user_id)
            if row is None:
                raise UserNotFoundError(user_id)
            return _to_user(row)

    async def set_status(self, user_id: str, status: UserStatus) -> None:
        async with write_session(self._session_factory) as session:
            row = await session.get(UserRow, user_id)
            if row is None:
                raise UserNotFoundError(user_id)
            row.status = status.value

    async def try_activate(self, user_id: str, max_active: int | None) -> bool:
        """Atomically activate a waiting person if the configured cap has room."""
        conditions = [UserRow.id == user_id, UserRow.status == UserStatus.WAITING.value]
        if max_active is not None:
            active_count = (
                select(func.count())
                .select_from(UserRow)
                .where(UserRow.status == UserStatus.ACTIVE.value)
                .scalar_subquery()
            )
            conditions.append(active_count < max_active)
        async with write_session(self._session_factory) as session:
            if _needs_activation_lock(session, max_active):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('users_activation'))")
                )
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(UserRow).where(*conditions).values(status=UserStatus.ACTIVE.value)
                ),
            )
            return result.rowcount == 1

    async def count_by_status(self) -> dict[UserStatus, int]:
        async with read_session(self._session_factory) as session:
            pairs = (
                await session.execute(select(UserRow.status, func.count()).group_by(UserRow.status))
            ).all()
        counts = dict.fromkeys(UserStatus, 0)
        for status, count in pairs:
            counts[UserStatus(status)] = count
        return counts

    async def list(self) -> UserList:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(select(UserRow).order_by(UserRow.created_at.desc()))
            return [_to_user(row) for row in rows.all()]


def _needs_activation_lock(session: AsyncSession, max_active: int | None) -> bool:
    return (
        max_active is not None
        and session.bind is not None
        and session.bind.dialect.name == "postgresql"
    )


def _to_user(row: UserRow) -> User:
    return User(
        id=row.id,
        name=row.name,
        email=row.email or "",
        status=UserStatus(row.status),
        created_at=row.created_at,
    )
