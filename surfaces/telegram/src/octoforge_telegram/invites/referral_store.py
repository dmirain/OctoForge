"""SQL referral codes and immutable who-brought-whom claims."""

import secrets

from octoforge_core.time import utc_now
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_telegram.invites.models import ReferralClaimRow, ReferralCodeRow
from octoforge_telegram.invites.types import ReferralClaim

CODE_BYTES = 8


class SqlAlchemyReferralStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def code_of(self, owner_user_id: str) -> str:
        async with self._sessions() as session:
            row = (
                await session.scalars(
                    select(ReferralCodeRow).where(ReferralCodeRow.owner_user_id == owner_user_id)
                )
            ).first()
            if row is not None:
                return row.code
            row = ReferralCodeRow(
                code=secrets.token_urlsafe(CODE_BYTES),
                owner_user_id=owner_user_id,
                created_at=utc_now(),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                won = (
                    await session.scalars(
                        select(ReferralCodeRow).where(
                            ReferralCodeRow.owner_user_id == owner_user_id
                        )
                    )
                ).first()
                assert won is not None
                return won.code
            return row.code

    async def owner_of(self, code: str) -> str | None:
        async with self._sessions() as session:
            row = await session.get(ReferralCodeRow, code)
            return row.owner_user_id if row is not None else None

    async def record_claim(self, user_id: str, referrer_user_id: str, code: str) -> None:
        async with self._sessions() as session:
            if await session.get(ReferralClaimRow, user_id) is not None:
                return
            session.add(
                ReferralClaimRow(
                    user_id=user_id,
                    referrer_user_id=referrer_user_id,
                    code=code,
                    claimed_at=utc_now(),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    async def claim_of(self, user_id: str) -> ReferralClaim | None:
        async with self._sessions() as session:
            row = await session.get(ReferralClaimRow, user_id)
            return _to_claim(row) if row is not None else None

    async def claims(self) -> list[ReferralClaim]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(ReferralClaimRow).order_by(ReferralClaimRow.claimed_at)
                )
            ).all()
            return [_to_claim(row) for row in rows]


def _to_claim(row: ReferralClaimRow) -> ReferralClaim:
    return ReferralClaim(row.user_id, row.referrer_user_id, row.code, row.claimed_at)
