"""SQL persistence and atomic claiming of Telegram invites."""

import secrets
import uuid
from datetime import timedelta
from typing import Any, cast

from octoforge_core.time import utc_now
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_telegram.invites._invite_rows import get_invite_row, to_invite
from octoforge_telegram.invites.models import InviteRow
from octoforge_telegram.invites.types import (
    Invite,
    InviteAlreadyClaimedError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteStatus,
)

CODE_BYTES = 8


class SqlAlchemyInviteStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ttl_seconds: float | None = None,
    ) -> None:
        self._sessions = session_factory
        self._ttl_seconds = ttl_seconds

    async def create(self, note: str) -> Invite:
        async with self._sessions() as session:
            row = InviteRow(
                id=uuid.uuid4().hex,
                code=secrets.token_urlsafe(CODE_BYTES),
                status=InviteStatus.PENDING.value,
                note=note,
                created_at=utc_now(),
            )
            session.add(row)
            await session.commit()
            return to_invite(row)

    async def get_by_code(self, code: str) -> Invite | None:
        async with self._sessions() as session:
            row = (await session.scalars(select(InviteRow).where(InviteRow.code == code))).first()
            return to_invite(row) if row is not None else None

    async def get_by_id(self, invite_id: str) -> Invite | None:
        async with self._sessions() as session:
            row = await session.get(InviteRow, invite_id)
            return to_invite(row) if row is not None else None

    async def claim(self, code: str, user_id: str) -> Invite:
        async with self._sessions() as session:
            row = (await session.scalars(select(InviteRow).where(InviteRow.code == code))).first()
            if row is None:
                raise InviteNotFoundError(code)
            now = utc_now()
            conditions = [InviteRow.code == code, InviteRow.status == InviteStatus.PENDING.value]
            if self._ttl_seconds is not None:
                conditions.append(InviteRow.created_at > now - timedelta(seconds=self._ttl_seconds))
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    update(InviteRow)
                    .where(*conditions)
                    .values(status=InviteStatus.CLAIMED.value, claimed_by=user_id, claimed_at=now)
                ),
            )
            if result.rowcount == 0:
                if row.status == InviteStatus.PENDING.value:
                    raise InviteExpiredError(code)
                raise InviteAlreadyClaimedError(code)
            await session.commit()
            claimed = await session.get(InviteRow, row.id)
            assert claimed is not None
            return to_invite(claimed)

    async def get_by_user(self, user_id: str) -> Invite | None:
        async with self._sessions() as session:
            row = (
                await session.scalars(select(InviteRow).where(InviteRow.claimed_by == user_id))
            ).first()
            return to_invite(row) if row is not None else None

    async def list_all(self) -> list[Invite]:
        async with self._sessions() as session:
            rows = (await session.scalars(select(InviteRow).order_by(InviteRow.created_at))).all()
            return [to_invite(row) for row in rows]

    async def revoke(self, invite_id: str) -> Invite:
        return await self._set_revoked(invite_id, True)

    async def restore(self, invite_id: str) -> Invite:
        return await self._set_revoked(invite_id, False)

    async def set_disabled_cron_jobs(self, invite_id: str, job_ids: tuple[str, ...]) -> None:
        async with self._sessions() as session:
            row = await get_invite_row(session, invite_id)
            row.disabled_cron_job_ids = list(job_ids)
            await session.commit()

    async def _set_revoked(self, invite_id: str, revoked: bool) -> Invite:
        async with self._sessions() as session:
            row = await get_invite_row(session, invite_id)
            row.status = (InviteStatus.REVOKED if revoked else InviteStatus.CLAIMED).value
            row.revoked_at = utc_now() if revoked else None
            await session.commit()
            return to_invite(row)
