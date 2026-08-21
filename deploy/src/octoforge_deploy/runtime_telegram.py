"""Telegram surface composition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from octoforge_core.identity.api import IdentityStore
from octoforge_core.identity.service import AccessService
from octoforge_core.media.api import MediaUnderstanding
from octoforge_core.tools.base import Tool
from octoforge_telegram.bridge import RunnerProvider, TelegramBridgeOptions
from octoforge_telegram.client import TelegramBotClient
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.poller import (
    BridgeRegistryServices,
    MembershipOptions,
    TelegramBridgeRegistry,
    TelegramMembership,
    TelegramPoller,
    TelegramPollerOptions,
)
from octoforge_telegram.surface import TelegramSurface, TelegramSurfaceConfig

from octoforge_deploy.runtime_http import HttpClients
from octoforge_deploy.runtime_telegram_storage import TelegramStores


@dataclass(frozen=True, slots=True)
class TelegramExtras:
    stores: TelegramStores | None = None
    secrets_link: Callable[[str], str] | None = None
    admin_tool: Tool | None = None
    identities: IdentityStore | None = None
    media: MediaUnderstanding | None = None
    access: AccessService | None = None


@dataclass(frozen=True, slots=True)
class TelegramBuildRequest:
    settings: TelegramSettings
    runner_provider: RunnerProvider
    extras: TelegramExtras


def build_telegram_surface(
    request: TelegramBuildRequest,
    clients: HttpClients,
) -> TelegramSurface | None:
    settings, extras = request.settings, request.extras
    if not settings.telegram_bot_token:
        return None
    client = TelegramBotClient(clients.outbound, settings.telegram_bot_token)
    registry = TelegramBridgeRegistry(
        BridgeRegistryServices(request.runner_provider, client, extras.identities),
        TelegramBridgeOptions(
            settings.telegram_edit_throttle_seconds,
            extras.stores.drafts if extras.stores is not None else None,
        ),
    )
    membership = _membership(settings, extras.stores)
    poller = TelegramPoller(
        client,
        registry,
        TelegramPollerOptions(
            poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
            membership=membership,
            secrets_link=extras.secrets_link,
            directory=extras.stores.directory if extras.stores is not None else None,
            identities=extras.identities,
            media=extras.media,
            access=extras.access,
            referrals=extras.stores.referrals if extras.stores is not None else None,
            bot_username=settings.resolved_bot_username(),
            voice_max_seconds=settings.voice_max_seconds,
        ),
    )
    return TelegramSurface(
        registry,
        poller,
        TelegramSurfaceConfig(extras.admin_tool, settings.telegram_poll_in_process),
    )


def _membership(
    settings: TelegramSettings,
    stores: TelegramStores | None,
) -> TelegramMembership | None:
    if stores is None or not settings.telegram_admin_ids:
        return None
    return TelegramMembership(
        stores.invites,
        MembershipOptions(
            frozenset(settings.telegram_admin_ids),
            stores.referrals,
            settings.telegram_open_registration,
        ),
    )
