"""SQLAlchemy implementation of the UserParamStore port."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.params.api import (
    UserParam,
    UserParamNotFoundError,
    normalize_code,
    normalize_value,
)
from octoforge_core.params.models import UserParamRow
from octoforge_core.time import utc_now


class SqlAlchemyUserParamStore:
    """SQL persistence for per-user non-secret parameters."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def put(self, user_id: str, code: str, value: str) -> UserParam:
        """Store or replace the user's param under `code`."""
        code = normalize_code(code)
        value = normalize_value(value)
        async with write_session(self._session_factory) as session:
            row = await _find_row(session, user_id, code)
            if row is None:
                row = UserParamRow(id=uuid.uuid4().hex, user_id=user_id, code=code, value=value)
                session.add(row)
            else:
                row.value = value
                row.updated_at = utc_now()
            await session.flush()
            return _to_param(row)

    async def get_for_user(self, user_id: str) -> dict[str, str]:
        """Return every param of the user as a code→value mapping."""
        async with read_session(self._session_factory) as session:
            rows = (
                await session.scalars(select(UserParamRow).where(UserParamRow.user_id == user_id))
            ).all()
            return {row.code: row.value for row in rows}

    async def list(self, user_id: str) -> list[UserParam]:
        """Return the user's params ordered by code."""
        async with read_session(self._session_factory) as session:
            rows = (
                await session.scalars(
                    select(UserParamRow)
                    .where(UserParamRow.user_id == user_id)
                    .order_by(UserParamRow.code)
                )
            ).all()
            return [_to_param(row) for row in rows]

    async def delete(self, user_id: str, code: str) -> None:
        """Delete the user's param by code; raise `UserParamNotFoundError`."""
        code = normalize_code(code)
        async with write_session(self._session_factory) as session:
            row = await _find_row(session, user_id, code)
            if row is None:
                raise UserParamNotFoundError(code)
            await session.delete(row)


async def _find_row(session: AsyncSession, user_id: str, code: str) -> UserParamRow | None:
    result = await session.scalars(
        select(UserParamRow).where(UserParamRow.user_id == user_id, UserParamRow.code == code)
    )
    return result.first()


def _to_param(row: UserParamRow) -> UserParam:
    return UserParam(
        code=row.code,
        value=row.value,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
