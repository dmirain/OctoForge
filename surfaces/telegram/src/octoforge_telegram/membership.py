"""Telegram invite/referral gate before person admission."""

import logging
from dataclasses import dataclass
from enum import StrEnum

from octoforge_telegram.client import USER_ID_PREFIX
from octoforge_telegram.invites.api import (
    InviteAlreadyClaimedError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteStatus,
    InviteStore,
    ReferralStore,
)

logger = logging.getLogger("octoforge_telegram.poller")
COMMAND_START = "/start"
REFERRAL_PREFIX = "ref_"


class MembershipDecision(StrEnum):
    ALLOW = "allow"
    ALLOW_WITH_WELCOME = "allow_with_welcome"
    DENY_INVITE_INVALID = "deny_invite_invalid"
    DENY_NO_ACCESS = "deny_no_access"


@dataclass(frozen=True, slots=True)
class MembershipOptions:
    admin_ids: frozenset[int]
    referrals: ReferralStore | None = None
    open_registration: bool = False


class TelegramMembership:
    def __init__(self, invite_store: InviteStore, options: MembershipOptions) -> None:
        self._invites = invite_store
        self._admin_ids = options.admin_ids
        self._referrals = options.referrals
        self._open = options.open_registration

    async def check(self, user_id: str, text: str) -> MembershipDecision:
        numeric_id = chat_id_from_user_id(user_id)
        if numeric_id is not None and numeric_id in self._admin_ids:
            return MembershipDecision.ALLOW
        decision = await self._decide(user_id, text)
        if self._open and decision is not MembershipDecision.ALLOW_WITH_WELCOME:
            return MembershipDecision.ALLOW
        return decision

    async def _decide(self, user_id: str, text: str) -> MembershipDecision:
        code = start_code(text)
        if code is not None and code.startswith(REFERRAL_PREFIX) and self._referrals is not None:
            return await self._claim_referral(code.removeprefix(REFERRAL_PREFIX), user_id)
        if code is not None:
            return await self._claim(code, user_id)
        return (
            MembershipDecision.ALLOW
            if await self._is_member(user_id)
            else MembershipDecision.DENY_NO_ACCESS
        )

    async def _is_member(self, user_id: str) -> bool:
        invite = await self._invites.get_by_user(user_id)
        if invite is not None and invite.status is InviteStatus.CLAIMED:
            return True
        return self._referrals is not None and await self._referrals.claim_of(user_id) is not None

    async def _claim(self, code: str, user_id: str) -> MembershipDecision:
        try:
            await self._invites.claim(code, user_id)
        except (InviteAlreadyClaimedError, InviteExpiredError, InviteNotFoundError):
            return MembershipDecision.DENY_INVITE_INVALID
        return MembershipDecision.ALLOW_WITH_WELCOME

    async def _claim_referral(self, code: str, user_id: str) -> MembershipDecision:
        assert self._referrals is not None
        owner = await self._referrals.owner_of(code)
        if owner is None or owner == user_id:
            logger.warning(
                "referral: %s presented %s code %r; no attribution",
                user_id,
                "their own" if owner is not None else "an unknown",
                code,
            )
            return MembershipDecision.DENY_INVITE_INVALID
        if await self._is_member(user_id):
            return MembershipDecision.ALLOW
        await self._referrals.record_claim(user_id, owner, code)
        return MembershipDecision.ALLOW_WITH_WELCOME


def start_code(text: str) -> str | None:
    if not text.startswith(COMMAND_START + " "):
        return None
    return text[len(COMMAND_START) :].strip() or None


def chat_id_from_user_id(user_id: str) -> int | None:
    if not user_id.startswith(USER_ID_PREFIX):
        return None
    try:
        return int(user_id.removeprefix(USER_ID_PREFIX))
    except ValueError:
        return None
