"""Telegram poller queues, profile mirror and optional services."""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from octoforge_core.identity.api import IdentityProfile
from octoforge_core.identity.service import AccessService
from octoforge_core.media.api import MediaUnderstanding

from octoforge_telegram.gateway import GatewayRegistry
from octoforge_telegram.invites.api import MemberDirectory, ReferralStore
from octoforge_telegram.membership import TelegramMembership
from octoforge_telegram.models import TelegramMessage

ALBUM_QUIET_SECONDS = 1.5
DEFAULT_VOICE_MAX_SECONDS = 600.0


@dataclass(slots=True)
class Inbox:
    pending: deque[TelegramMessage] = field(default_factory=deque)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task[None] | None = None
    busy: bool = False


class ProfileMirror(Protocol):
    async def update_profile(self, profile: IdentityProfile) -> None: ...


@dataclass(frozen=True, slots=True)
class TelegramPollerOptions:
    poll_timeout_seconds: float
    error_backoff_seconds: float = 5.0
    membership: TelegramMembership | None = None
    secrets_link: Callable[[str], str] | None = None
    directory: MemberDirectory | None = None
    identities: ProfileMirror | None = None
    media: MediaUnderstanding | None = None
    access: AccessService | None = None
    referrals: ReferralStore | None = None
    bot_username: str | None = None
    album_quiet_seconds: float = ALBUM_QUIET_SECONDS
    voice_max_seconds: float = DEFAULT_VOICE_MAX_SECONDS


@dataclass(frozen=True, slots=True)
class PollerServices:
    registry: GatewayRegistry
