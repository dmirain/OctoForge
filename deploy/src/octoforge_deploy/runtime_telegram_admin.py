"""Telegram's deployment-level administration tool."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from octoforge_core.cron.api import CronStore
from octoforge_core.dialogs.api import DialogRepository, MessageRepository
from octoforge_core.identity.api import IdentityStore
from octoforge_core.instructions.api import InstructionService
from octoforge_core.tools.base import Tool
from octoforge_telegram.admin import AdminAccess, AdminManageTool, AdminStores
from octoforge_telegram.client import TELEGRAM_CHANNEL, TelegramBotClient
from octoforge_telegram.config import TelegramSettings

from octoforge_deploy.runtime_http import HttpClients
from octoforge_deploy.runtime_telegram_storage import TelegramStores

ADMIN_NOT_SEEN_MESSAGE = (
    "%d of %d OF_TELEGRAM_ADMIN_IDS have never messaged this bot, so they have no "
    "person to be an admin as and admin_manage stays hidden from them; it appears "
    "after they write once and this process restarts"
)
logger = logging.getLogger("octoforge_deploy.main")


@dataclass(frozen=True, slots=True)
class AdminToolServices:
    cron: CronStore
    dialogs: DialogRepository
    messages: MessageRepository
    instructions: InstructionService
    identities: IdentityStore


@dataclass(frozen=True, slots=True)
class AdminToolRequest:
    settings: TelegramSettings
    stores: TelegramStores | None
    services: AdminToolServices


async def build_admin_tool(
    request: AdminToolRequest,
    clients: HttpClients,
) -> Tool | None:
    if request.stores is None or not request.settings.telegram_admin_ids:
        return None
    services = request.services
    return AdminManageTool(
        AdminStores(
            invites=request.stores.invites,
            cron_store=services.cron,
            messages=services.messages,
            instructions=services.instructions,
            identities=services.identities,
            directory=request.stores.directory,
        ),
        AdminAccess(
            admin_user_ids=await _admin_user_ids(
                services.identities,
                request.settings.telegram_admin_ids,
            ),
            telegram=TelegramBotClient(
                clients.outbound,
                request.settings.telegram_bot_token,
            ),
            bot_username=request.settings.resolved_bot_username(),
        ),
    )


async def _admin_user_ids(
    identities: IdentityStore,
    admin_ids: Sequence[int],
) -> frozenset[str]:
    resolved: set[str] = set()
    unknown: list[int] = []
    for external in admin_ids:
        user_id = await identities.resolve(TELEGRAM_CHANNEL, str(external))
        if user_id is None:
            unknown.append(external)
        else:
            resolved.add(user_id)
    if unknown:
        logger.warning(ADMIN_NOT_SEEN_MESSAGE, len(unknown), len(admin_ids))
    return frozenset(resolved)
