"""SQL directory of Telegram member profiles."""

from octoforge_core.time import utc_now
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_telegram.invites.models import MemberRow
from octoforge_telegram.invites.types import MemberProfile, MemberProfileUpdate


class SqlAlchemyMemberDirectory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def record(self, profile: MemberProfileUpdate) -> None:
        now = utc_now()
        async with self._sessions() as session:
            row = await session.get(MemberRow, profile.user_id)
            if row is None:
                session.add(
                    MemberRow(
                        user_id=profile.user_id,
                        first_name=profile.first_name,
                        last_name=profile.last_name,
                        username=profile.username,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                row.first_name = profile.first_name
                row.last_name = profile.last_name
                row.username = profile.username
                row.last_seen_at = now
            await session.commit()

    async def get(self, user_id: str) -> MemberProfile | None:
        async with self._sessions() as session:
            row = await session.get(MemberRow, user_id)
            return _to_profile(row) if row is not None else None

    async def list_all(self) -> list[MemberProfile]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(select(MemberRow).order_by(MemberRow.last_seen_at.desc()))
            ).all()
            return [_to_profile(row) for row in rows]


def _to_profile(row: MemberRow) -> MemberProfile:
    return MemberProfile(
        row.user_id,
        row.first_name,
        row.last_name,
        row.username,
        row.first_seen_at,
        row.last_seen_at,
    )
