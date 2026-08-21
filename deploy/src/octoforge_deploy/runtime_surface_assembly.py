"""Build installed surfaces after the core graph exists."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from octoforge_core import ConversationManager
from octoforge_core.tools.base import Tool
from octoforge_core.tools.registry import ToolRegistry
from octoforge_server.config import Settings
from octoforge_server.secret_links import secrets_link_builder
from octoforge_server.surfaces import Surface
from octoforge_telegram.client import TELEGRAM_CHANNEL
from octoforge_telegram.config import TelegramSettings

from octoforge_deploy.runtime_database import DatabaseRuntime
from octoforge_deploy.runtime_http import HttpClients
from octoforge_deploy.runtime_knowledge import KnowledgeServices
from octoforge_deploy.runtime_media import MediaServices, PersonScopedMedia
from octoforge_deploy.runtime_surfaces import attach_renderers, installed_surfaces
from octoforge_deploy.runtime_telegram import (
    TelegramBuildRequest,
    TelegramExtras,
    build_telegram_surface,
)
from octoforge_deploy.runtime_telegram_admin import (
    AdminToolRequest,
    AdminToolServices,
    build_admin_tool,
)


@dataclass(frozen=True, slots=True)
class SurfaceBuildRequest:
    settings: Settings
    telegram: TelegramSettings
    database: DatabaseRuntime
    knowledge: KnowledgeServices
    media: MediaServices
    manager: ConversationManager
    registry: ToolRegistry


async def build_surfaces(
    request: SurfaceBuildRequest,
    clients: HttpClients,
) -> tuple[Surface, ...]:
    admin = await _build_admin(request, clients)
    stores = request.database.stores
    telegram = build_telegram_surface(
        TelegramBuildRequest(
            request.telegram,
            request.manager.get_or_create_runner,
            TelegramExtras(
                stores=request.database.telegram,
                secrets_link=_secrets_link(request),
                admin_tool=admin,
                identities=stores.identity,
                media=PersonScopedMedia(
                    request.media.service,
                    stores.identity,
                    TELEGRAM_CHANNEL,
                ),
                access=stores.access,
            ),
        ),
        clients,
    )
    return tuple(
        attach_renderers(
            installed_surfaces(telegram),
            request.manager,
            request.registry,
        ),
    )


async def _build_admin(
    request: SurfaceBuildRequest,
    clients: HttpClients,
) -> Tool | None:
    stores = request.database.stores
    return await build_admin_tool(
        AdminToolRequest(
            request.telegram,
            request.database.telegram,
            AdminToolServices(
                stores.cron,
                stores.manager.dialogs,
                stores.manager.messages,
                request.knowledge.instructions,
                stores.identity,
            ),
        ),
        clients,
    )


def _secrets_link(request: SurfaceBuildRequest) -> Callable[[str], str] | None:
    stores = request.database.stores
    if stores.secrets is None:
        return None
    return secrets_link_builder(
        request.settings,
        stores.secret_links,
        TELEGRAM_CHANNEL,
    )
