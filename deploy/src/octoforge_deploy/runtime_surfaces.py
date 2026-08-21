"""Installed interface declarations and failure-isolated lifecycle."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from octoforge_console import surface as console
from octoforge_console.surface import ConsoleSurface
from octoforge_core import ConversationManager
from octoforge_core.tools.registry import ToolRegistry
from octoforge_server.app import SECRETS_PAGE
from octoforge_server.surfaces import Surface, SurfaceRoutes
from octoforge_telegram import surface as telegram_surface
from octoforge_telegram.surface import TelegramSurface
from octoforge_webui import surface as webui
from octoforge_webui.surface import WebUiSurface

from octoforge_deploy.runtime_telegram_storage import TelegramStores

logger = logging.getLogger("octoforge_deploy.main")


def surface_routes() -> tuple[SurfaceRoutes, ...]:
    return (
        SurfaceRoutes(
            name=console.SURFACE_NAME,
            routers=console.ROUTERS,
            static_files=console.STATIC_FILES,
        ),
        SurfaceRoutes(
            name=webui.SURFACE_NAME,
            routers=webui.ROUTERS,
            static_files=webui.STATIC_FILES,
        ),
        SurfaceRoutes(
            name=telegram_surface.SURFACE_NAME,
            routers=telegram_surface.ROUTERS,
            static_files=telegram_surface.STATIC_FILES,
        ),
        SurfaceRoutes(name="secrets", static_files=(SECRETS_PAGE,)),
    )


def installed_surfaces(telegram: TelegramSurface | None) -> tuple[Surface, ...]:
    surfaces: list[Surface] = [ConsoleSurface(), WebUiSurface()]
    if telegram is not None:
        surfaces.append(telegram)
    return tuple(surfaces)


def attach_renderers(
    surfaces: Sequence[Surface],
    manager: ConversationManager,
    registry: ToolRegistry,
) -> Sequence[Surface]:
    for surface in surfaces:
        renderer = surface.dialog_surface()
        if renderer is not None:
            manager.use_surface(renderer)
        for tool in surface.tools():
            registry.register(tool)
    return surfaces


async def start_surfaces(surfaces: Sequence[Surface]) -> None:
    for surface in surfaces:
        try:
            await surface.start()
        except Exception:
            logger.exception("surface failed to start: %s", surface.name)


async def close_surface(surface: Surface) -> None:
    try:
        await surface.aclose()
    except Exception:
        logger.exception("surface failed to close: %s", surface.name)


def served_channels(surfaces: Sequence[Surface]) -> frozenset[str]:
    return frozenset(
        channel for channel in (surface.channel() for surface in surfaces) if channel is not None
    )


def surface_state(stores: TelegramStores | None) -> dict[str, Any]:
    return {
        "telegram_members": stores.directory if stores is not None else None,
        "telegram_invites": stores.invites if stores is not None else None,
        "telegram_referrals": stores.referrals if stores is not None else None,
    }
