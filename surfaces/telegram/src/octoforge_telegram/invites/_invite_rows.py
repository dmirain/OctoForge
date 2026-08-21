"""Lookup and mapping of persisted Telegram invites."""

from sqlalchemy.ext.asyncio import AsyncSession

from octoforge_telegram.invites.models import InviteRow
from octoforge_telegram.invites.types import Invite, InviteNotFoundError, InviteStatus


async def get_invite_row(session: AsyncSession, invite_id: str) -> InviteRow:
    row = await session.get(InviteRow, invite_id)
    if row is None:
        raise InviteNotFoundError(invite_id)
    return row


def to_invite(row: InviteRow) -> Invite:
    return Invite(
        row.id,
        row.code,
        InviteStatus(row.status),
        row.note,
        row.created_at,
        row.claimed_at,
        row.revoked_at,
        row.claimed_by,
        tuple(row.disabled_cron_job_ids or ()),
    )
