"""Invite, status and profile gates for one Telegram sender."""

import logging
from dataclasses import dataclass

from octoforge_core.identity.api import IdentityKey, IdentityProfile, UserStatus
from octoforge_core.identity.service import AccessService
from octoforge_core.time import utc_now

from octoforge_telegram.client import TELEGRAM_CHANNEL, USER_ID_PREFIX, SendMessage, TelegramClient
from octoforge_telegram.gateway import GatewayRegistry
from octoforge_telegram.invites.api import MemberDirectory, MemberProfileUpdate
from octoforge_telegram.membership import MembershipDecision, TelegramMembership
from octoforge_telegram.models import TelegramMessage, TelegramUser, display_name
from octoforge_telegram.poller_text import (
    ACCESS_CLOSED_TEXT,
    ACCESS_DENIED_TEXT,
    INVITE_INVALID_TEXT,
    QUEUE_WAIT_TEXT,
    WELCOME_TEXT,
)
from octoforge_telegram.poller_types import ProfileMirror

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AccessServices:
    client: TelegramClient
    registry: GatewayRegistry
    membership: TelegramMembership | None
    directory: MemberDirectory | None
    identities: ProfileMirror | None
    access: AccessService | None


class PollerAccess:
    def __init__(self, services: AccessServices) -> None:
        self._client = services.client
        self._registry = services.registry
        self._membership = services.membership
        self._directory = services.directory
        self._identities = services.identities
        self._access = services.access
        self._status_notes: dict[tuple[str, str], str] = {}

    async def gate(self, user_id: str, chat_id: int, message: TelegramMessage) -> bool:
        if not await self._check_membership(user_id, chat_id, message.body or ""):
            return False
        assert message.from_user is not None
        await self._record_member(user_id, message.from_user)
        if self._access is None:
            return True
        person = await self._registry.person_of(user_id.removeprefix(USER_ID_PREFIX))
        status = await self._access.admit(person)
        if status is UserStatus.ACTIVE:
            return True
        await self.say_status(person, chat_id, status)
        return False

    async def say_status(self, person: str, chat_id: int, status: UserStatus) -> None:
        notice = ACCESS_CLOSED_TEXT if status is UserStatus.BANNED else QUEUE_WAIT_TEXT
        key = (person, notice)
        day = utc_now().strftime("%Y-%m-%d")
        if self._status_notes.get(key) != day:
            self._status_notes[key] = day
            await self._client.send_message(SendMessage(chat_id, notice))

    async def _check_membership(self, user_id: str, chat_id: int, text: str) -> bool:
        if self._membership is None:
            return True
        decision = await self._membership.check(user_id, text)
        if decision is MembershipDecision.ALLOW:
            return True
        if decision is MembershipDecision.ALLOW_WITH_WELCOME:
            await self._client.send_message(SendMessage(chat_id, WELCOME_TEXT))
            return True
        if decision is MembershipDecision.DENY_INVITE_INVALID:
            await self._client.send_message(SendMessage(chat_id, INVITE_INVALID_TEXT))
            return False
        await self._client.send_message(SendMessage(chat_id, ACCESS_DENIED_TEXT))
        return False

    async def _record_member(self, user_id: str, user: TelegramUser) -> None:
        if self._directory is not None:
            try:
                await self._directory.record(
                    MemberProfileUpdate(user_id, user.first_name, user.last_name, user.username)
                )
            except Exception:
                logger.exception("member profile record failed: user=%s", user_id)
        if self._identities is not None:
            try:
                external_id = user_id.removeprefix(USER_ID_PREFIX)
                await self._registry.person_of(external_id)
                await self._identities.update_profile(
                    IdentityProfile(
                        IdentityKey(TELEGRAM_CHANNEL, external_id),
                        display_name(user),
                        user.username,
                    )
                )
            except Exception:
                logger.exception("identity profile update failed: user=%s", user_id)
