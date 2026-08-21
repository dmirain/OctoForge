"""Assemble the standalone Telegram ingestion node and report service media."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack

import httpx
from octoforge_core.db.engine import create_engine, create_session_factory
from octoforge_server.config import Settings
from octoforge_server.secret_links import SecretLinkService, secrets_link_builder

from octoforge_telegram.client import TELEGRAM_CHANNEL, TelegramBotClient
from octoforge_telegram.config import TelegramSettings
from octoforge_telegram.gateway import ApiGatewayRegistry, ApiProfileMirror, basic_auth_header
from octoforge_telegram.invites.store import (
    SqlAlchemyInviteStore,
    SqlAlchemyMemberDirectory,
    SqlAlchemyReferralStore,
)
from octoforge_telegram.media_client import ApiMediaUnderstanding
from octoforge_telegram.poller import (
    MembershipOptions,
    TelegramMembership,
    TelegramPoller,
    TelegramPollerOptions,
)
from octoforge_telegram.schema import TelegramSurfaceBase

logger = logging.getLogger(__name__)
MEDIA_PROBE_ATTEMPTS = 6
MEDIA_PROBE_DELAY_SECONDS = 5.0


def service_headers(settings: Settings) -> dict[str, str]:
    return basic_auth_header(settings.service_username, settings.service_password)


async def build(
    stack: AsyncExitStack,
    settings: Settings,
    telegram: TelegramSettings,
) -> TelegramPoller:
    outbound = await stack.enter_async_context(httpx.AsyncClient())
    service = await stack.enter_async_context(
        httpx.AsyncClient(
            base_url=telegram.telegram_service_url,
            headers=service_headers(settings),
        )
    )
    engine = create_engine(telegram.telegram_database_url)
    stack.push_async_callback(engine.dispose)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    sessions = create_session_factory(engine)
    invites = SqlAlchemyInviteStore(sessions, ttl_seconds=telegram.telegram_invite_ttl_seconds)
    referrals = SqlAlchemyReferralStore(sessions)
    membership = (
        TelegramMembership(
            invites,
            MembershipOptions(
                frozenset(telegram.telegram_admin_ids),
                referrals,
                telegram.telegram_open_registration,
            ),
        )
        if telegram.telegram_admin_ids
        else None
    )
    media = ApiMediaUnderstanding(service)
    await report_media(media)
    return TelegramPoller(
        TelegramBotClient(outbound, telegram.telegram_bot_token),
        ApiGatewayRegistry(service),
        TelegramPollerOptions(
            poll_timeout_seconds=telegram.telegram_poll_timeout_seconds,
            membership=membership,
            directory=SqlAlchemyMemberDirectory(sessions),
            identities=ApiProfileMirror(service),
            media=media,
            voice_max_seconds=telegram.voice_max_seconds,
            secrets_link=_secrets_link(settings),
            referrals=referrals,
            bot_username=telegram.resolved_bot_username(),
        ),
    )


async def report_media(media: ApiMediaUnderstanding) -> None:
    capabilities = None
    for attempt in range(1, MEDIA_PROBE_ATTEMPTS + 1):
        capabilities = await media.capabilities()
        if capabilities is not None:
            break
        if attempt < MEDIA_PROBE_ATTEMPTS:
            await asyncio.sleep(MEDIA_PROBE_DELAY_SECONDS)
    if capabilities is None:
        logger.warning("media understanding: service did not answer")
        return
    logger.info(
        "media understanding (on the service): images %s, voice %s",
        "on" if capabilities.get("describes_images") else "off",
        "on" if capabilities.get("transcribes_audio") else "off",
    )


def _secrets_link(settings: Settings) -> Callable[[str], str] | None:
    if not settings.secrets_key:
        return None
    return secrets_link_builder(
        settings,
        SecretLinkService(settings.secrets_key),
        TELEGRAM_CHANNEL,
    )
