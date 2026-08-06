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


@dataclass(frozen=True, slots=True)
class MemberProfile:
    """What Telegram last told us about a gated user."""

    user_id: str
    first_name: str
    last_name: str
    username: str | None
    first_seen_at: datetime
    last_seen_at: datetime

    @property
    def display_name(self) -> str:
        """Human-readable name: "First Last (@username)" with graceful gaps."""
        name = " ".join(part for part in (self.first_name, self.last_name) if part)
        handle = f"@{self.username}" if self.username else ""
        if name and handle:
            return f"{name} ({handle})"
        return name or handle or self.user_id


class MemberDirectory(Protocol):
    """Storage port for member profiles (the who-is-who of the bot)."""

    async def record(
        self, user_id: str, first_name: str, last_name: str, username: str | None
    ) -> None:
        """Upsert the profile as seen on this contact; stamps last_seen_at."""
        ...

    async def get(self, user_id: str) -> MemberProfile | None:
        """Return the profile, None when the user was never recorded."""
        ...

    async def list_all(self) -> list[MemberProfile]:
        """Return every recorded profile, most recently seen first."""
        ...


@dataclass(frozen=True, slots=True)
class ReferralClaim:
    """Who brought whom: one attribution, written at first entry, never rewritten."""

    user_id: str
    referrer_user_id: str
    code: str
    claimed_at: datetime


class ReferralStore(Protocol):
    """Personal reusable referral codes and the who-brought-whom trail.

    A referral is attribution plus a way through the invite gate — never a
    way past the active-user cap: the invitee queues like anyone else.
    """

    async def code_of(self, owner_user_id: str) -> str:
        """The owner's personal code, minted lazily on first ask, stable after."""
        ...

    async def owner_of(self, code: str) -> str | None:
        """Whose code this is (None: unknown — the gate treats it as invalid)."""
        ...

    async def record_claim(self, user_id: str, referrer_user_id: str, code: str) -> None:
        """Attribute the newcomer; a later entry never overwrites the first."""
        ...

    async def claim_of(self, user_id: str) -> ReferralClaim | None:
        """How this user came in (None: not through a referral)."""
        ...

    async def claims(self) -> list[ReferralClaim]:
        """Every attribution, oldest first (the operator's who-brought-whom)."""
        ...


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
