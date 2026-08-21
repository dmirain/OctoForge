"""Telegram invite, referral and member profile values."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class InviteStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class Invite:
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
class MemberProfileUpdate:
    user_id: str
    first_name: str
    last_name: str
    username: str | None


@dataclass(frozen=True, slots=True)
class MemberProfile:
    user_id: str
    first_name: str
    last_name: str
    username: str | None
    first_seen_at: datetime
    last_seen_at: datetime

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part)
        handle = f"@{self.username}" if self.username else ""
        if name and handle:
            return f"{name} ({handle})"
        return name or handle or self.user_id


@dataclass(frozen=True, slots=True)
class ReferralClaim:
    user_id: str
    referrer_user_id: str
    code: str
    claimed_at: datetime


class InviteNotFoundError(Exception):
    pass


class InviteAlreadyClaimedError(Exception):
    pass


class InviteExpiredError(Exception):
    pass
