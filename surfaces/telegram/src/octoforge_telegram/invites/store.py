"""SQLAlchemy-backed InviteStore over the dedicated Telegram database."""

import secrets
import uuid
from datetime import timedelta
from typing import Any, cast

from octoforge_core.time import utc_now
from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_telegram.invites.api import (
    Invite,
    InviteAlreadyClaimedError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteStatus,
    InviteStore,
    MemberDirectory,
    MemberProfile,
    ReferralClaim,
    ReferralStore,
)
from octoforge_telegram.invites.models import (
    InviteRow,
    MemberRow,
    ReferralClaimRow,
    ReferralCodeRow,
)

_CODE_BYTES = 8


class SqlAlchemyInviteStore(InviteStore):
    """Invite codes in the dedicated Telegram SQLite database.

    `ttl_seconds` bounds how long a pending code stays claimable (None =
    never expires); claimed/revoked invites are unaffected by the TTL.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ttl_seconds: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._ttl_seconds = ttl_seconds

    async def create(self, note: str) -> Invite:
        async with self._session_factory() as session:
            row = InviteRow(
                id=uuid.uuid4().hex,
                code=secrets.token_urlsafe(_CODE_BYTES),
                status=InviteStatus.PENDING.value,
                note=note,
                created_at=utc_now(),
            )
            session.add(row)
            await session.commit()
            return _to_invite(row)

    async def get_by_code(self, code: str) -> Invite | None:
        async with self._session_factory() as session:
            result = await session.scalars(select(InviteRow).where(InviteRow.code == code))
            row = result.first()
            return _to_invite(row) if row is not None else None

    async def get_by_id(self, invite_id: str) -> Invite | None:
        async with self._session_factory() as session:
            row = await session.get(InviteRow, invite_id)
            return _to_invite(row) if row is not None else None

    async def claim(self, code: str, user_id: str) -> Invite:
        async with self._session_factory() as session:
            invite = await session.scalars(select(InviteRow).where(InviteRow.code == code))
            row = invite.first()
            if row is None:
                raise InviteNotFoundError(code)
            now = utc_now()
            conditions = [InviteRow.code == code, InviteRow.status == InviteStatus.PENDING.value]
            if self._ttl_seconds is not None:
                conditions.append(InviteRow.created_at > now - timedelta(seconds=self._ttl_seconds))
            statement = (
                update(InviteRow)
                .where(*conditions)
                .values(status=InviteStatus.CLAIMED.value, claimed_by=user_id, claimed_at=now)
            )
            # DML executes into a CursorResult at runtime; the stubs type
            # AsyncSession.execute loosely, so narrow it for rowcount.
            result = cast(CursorResult[Any], await session.execute(statement))
            if result.rowcount == 0:
                if row.status == InviteStatus.PENDING.value:
                    raise InviteExpiredError(code)
                raise InviteAlreadyClaimedError(code)
            await session.commit()
            claimed = await session.get(InviteRow, row.id)
            assert claimed is not None  # updated above, same transaction
            return _to_invite(claimed)

    async def get_by_user(self, user_id: str) -> Invite | None:
        async with self._session_factory() as session:
            result = await session.scalars(select(InviteRow).where(InviteRow.claimed_by == user_id))
            row = result.first()
            return _to_invite(row) if row is not None else None

    async def list_all(self) -> list[Invite]:
        async with self._session_factory() as session:
            result = await session.scalars(select(InviteRow).order_by(InviteRow.created_at))
            return [_to_invite(row) for row in result.all()]

    async def revoke(self, invite_id: str) -> Invite:
        async with self._session_factory() as session:
            row = await _get_row(session, invite_id)
            row.status = InviteStatus.REVOKED.value
            row.revoked_at = utc_now()
            await session.commit()
            return _to_invite(row)

    async def restore(self, invite_id: str) -> Invite:
        async with self._session_factory() as session:
            row = await _get_row(session, invite_id)
            row.status = InviteStatus.CLAIMED.value
            row.revoked_at = None
            await session.commit()
            return _to_invite(row)

    async def set_disabled_cron_jobs(self, invite_id: str, job_ids: tuple[str, ...]) -> None:
        async with self._session_factory() as session:
            row = await _get_row(session, invite_id)
            row.disabled_cron_job_ids = list(job_ids)
            await session.commit()


async def _get_row(session: AsyncSession, invite_id: str) -> InviteRow:
    row = await session.get(InviteRow, invite_id)
    if row is None:
        raise InviteNotFoundError(invite_id)
    return row


def _to_invite(row: InviteRow) -> Invite:
    return Invite(
        id=row.id,
        code=row.code,
        status=InviteStatus(row.status),
        note=row.note,
        created_at=row.created_at,
        claimed_at=row.claimed_at,
        revoked_at=row.revoked_at,
        claimed_by=row.claimed_by,
        disabled_cron_job_ids=tuple(row.disabled_cron_job_ids or ()),
    )


class SqlAlchemyReferralStore(ReferralStore):
    """Referral codes and attributions in the same dedicated Telegram database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def code_of(self, owner_user_id: str) -> str:
        async with self._session_factory() as session:
            existing = await session.scalars(
                select(ReferralCodeRow).where(ReferralCodeRow.owner_user_id == owner_user_id)
            )
            row = existing.first()
            if row is not None:
                return row.code
            row = ReferralCodeRow(
                code=secrets.token_urlsafe(_CODE_BYTES),
                owner_user_id=owner_user_id,
                created_at=utc_now(),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                # two first asks at once: the loser reads the winner's code
                await session.rollback()
                retry = await session.scalars(
                    select(ReferralCodeRow).where(ReferralCodeRow.owner_user_id == owner_user_id)
                )
                won = retry.first()
                assert won is not None  # somebody's insert committed
                return won.code
            return row.code

    async def owner_of(self, code: str) -> str | None:
        async with self._session_factory() as session:
            row = await session.get(ReferralCodeRow, code)
            return row.owner_user_id if row is not None else None

    async def record_claim(self, user_id: str, referrer_user_id: str, code: str) -> None:
        async with self._session_factory() as session:
            if await session.get(ReferralClaimRow, user_id) is not None:
                return  # the first attribution stands
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
                await session.rollback()  # a concurrent first entry: same answer

    async def claim_of(self, user_id: str) -> ReferralClaim | None:
        async with self._session_factory() as session:
            row = await session.get(ReferralClaimRow, user_id)
            return _to_claim(row) if row is not None else None

    async def claims(self) -> list[ReferralClaim]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(ReferralClaimRow).order_by(ReferralClaimRow.claimed_at)
            )
            return [_to_claim(row) for row in result.all()]


def _to_claim(row: ReferralClaimRow) -> ReferralClaim:
    return ReferralClaim(
        user_id=row.user_id,
        referrer_user_id=row.referrer_user_id,
        code=row.code,
        claimed_at=row.claimed_at,
    )


class SqlAlchemyMemberDirectory(MemberDirectory):
    """Member profiles in the same dedicated Telegram database."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self, user_id: str, first_name: str, last_name: str, username: str | None
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session:
            row = await session.get(MemberRow, user_id)
            if row is None:
                session.add(
                    MemberRow(
                        user_id=user_id,
                        first_name=first_name,
                        last_name=last_name,
                        username=username,
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            else:
                row.first_name = first_name
                row.last_name = last_name
                row.username = username
                row.last_seen_at = now
            await session.commit()

    async def get(self, user_id: str) -> MemberProfile | None:
        async with self._session_factory() as session:
            row = await session.get(MemberRow, user_id)
            return _to_profile(row) if row is not None else None

    async def list_all(self) -> list[MemberProfile]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(MemberRow).order_by(MemberRow.last_seen_at.desc())
            )
            return [_to_profile(row) for row in result.all()]


def _to_profile(row: MemberRow) -> MemberProfile:
    return MemberProfile(
        user_id=row.user_id,
        first_name=row.first_name,
        last_name=row.last_name,
        username=row.username,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
    )
