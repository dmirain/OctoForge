"""Public boundary of the Telegram invite store.

Invites are a purely Telegram-specific concept: the store, its schema and its
own SQLite database live entirely in the web layer; core knows nothing about
them (different Base, different engine, no Alembic chain).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class InviteStatus(StrEnum):
    """Lifecycle of an invite code."""

    PENDING = "pending"
    CLAIMED = "claimed"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class Invite:
    """One invite code with its claim/revoke trail."""

    id: str
    code: str
    status: InviteStatus
    note: str
    created_at: datetime
    claimed_at: datetime | None
    revoked_at: datetime | None
    claimed_by: str | None
    disabled_cron_job_ids: tuple[str, ...]


class InviteNotFoundError(Exception):
    """No invite for the given code or id."""


class InviteAlreadyClaimedError(Exception):
    """The invite code was already used (or revoked)."""


class InviteExpiredError(Exception):
    """The invite code is still pending but its TTL has run out."""


class InviteStore(Protocol):
    """Storage port for invite codes (substitutable in tests)."""

    async def create(self, note: str) -> Invite:
        """Create a pending invite with a fresh code."""
        ...

    async def get_by_code(self, code: str) -> Invite | None:
        """Return the invite with this code, None when unknown."""
        ...

    async def get_by_id(self, invite_id: str) -> Invite | None:
        """Return the invite with this id, None when unknown."""
        ...

    async def claim(self, code: str, user_id: str) -> Invite:
        """Atomically mark the pending code as claimed by the user.

        The conditional UPDATE decides the race: only the first concurrent
        claim succeeds. Raises InviteNotFoundError for an unknown code,
        InviteAlreadyClaimedError for a code not in the pending state and
        InviteExpiredError for a pending code whose TTL has run out.
        """
        ...

    async def get_by_user(self, user_id: str) -> Invite | None:
        """Return the invite claimed by this user, None when there is none."""
        ...

    async def list_all(self) -> list[Invite]:
        """Return every invite, oldest first (revoked included)."""
        ...

    async def revoke(self, invite_id: str) -> Invite:
        """Move the invite to revoked; raise InviteNotFoundError when missing."""
        ...

    async def restore(self, invite_id: str) -> Invite:
        """Move a revoked invite back to claimed (same claimed_by, no new code)."""
        ...

    async def set_disabled_cron_jobs(self, invite_id: str, job_ids: tuple[str, ...]) -> None:
        """Store which cron jobs this revocation disabled (for a precise restore)."""
        ...
