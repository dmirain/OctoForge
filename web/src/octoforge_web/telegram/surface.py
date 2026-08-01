"""Telegram as an installed surface: what it adds to the service.

Everything Telegram contributes is declared here rather than wired into the
composition root by hand — the routes for its console page, the renderer for
its channel, the admin tool it gives the agent, and the polling it runs in
the background.

The point is not tidiness. As long as the root knows *how* Telegram is put
together, a deployment without it means editing the root, and "optional"
means "commented out". Behind this port, a deployment without Telegram is one
that does not construct this object.
"""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress

from octoforge_core.agent.runner import DialogSurface
from octoforge_core.dialogs.api import DialogRepository
from octoforge_core.tools.base import Tool

from octoforge_web.surfaces import StaticFile
from octoforge_web.telegram.admin_routes import router as telegram_admin_router
from octoforge_web.telegram.client import TELEGRAM_CHANNEL
from octoforge_web.telegram.poller import TelegramBridgeRegistry, TelegramPoller

logger = logging.getLogger(__name__)

SURFACE_NAME = "telegram"


#: Mounted while the app is built (see the `Surface` port on why routes are
#: constants). The console's Telegram page ships with the surface that can
#: answer it, not with the console.
ROUTERS = (telegram_admin_router,)
STATIC_FILES: tuple[StaticFile, ...] = ()


class TelegramSurface:
    """The Telegram bot, plugged into the service (`Surface`)."""

    def __init__(
        self,
        registry: TelegramBridgeRegistry,
        poller: TelegramPoller,
        dialogs: DialogRepository,
        admin_tool: Tool | None = None,
    ) -> None:
        self._registry = registry
        self._poller = poller
        self._dialogs = dialogs
        self._admin_tool = admin_tool
        self._task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return SURFACE_NAME

    def channel(self) -> str | None:
        """Dialogs of the Telegram channel are this surface's."""
        return TELEGRAM_CHANNEL

    def dialog_surface(self) -> DialogSurface | None:
        """The bridge registry renders dialogs of the Telegram channel."""
        return self._registry

    def tools(self) -> Sequence[Tool]:
        """`admin_manage`, when this deployment has admins to use it."""
        return () if self._admin_tool is None else (self._admin_tool,)

    async def start(self) -> None:
        """Warm bridges for known dialogs, then poll for updates."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(_report_failure)

    async def _run(self) -> None:
        user_ids = await self._dialogs.list_user_ids_by_channel(TELEGRAM_CHANNEL)
        await self._registry.warm(user_ids)
        await self._poller.run_forever()

    async def aclose(self) -> None:
        """Stop polling and close every bridge."""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._registry.aclose()


def _report_failure(task: asyncio.Task[None]) -> None:
    """Supervisor-lite: a surface dying silently would look like a quiet bot.

    Cancellation is the normal shutdown path and is not a failure.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.error("telegram surface stopped", exc_info=error)
