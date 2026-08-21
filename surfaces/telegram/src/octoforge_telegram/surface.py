"""Telegram's routes, renderer, tools and background work as one surface."""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.agent.runner import DialogSurface
from octoforge_core.tools.base import Tool
from octoforge_server.surfaces import StaticFile

from octoforge_telegram.admin_routes import router as telegram_admin_router
from octoforge_telegram.client import TELEGRAM_CHANNEL
from octoforge_telegram.poller import TelegramBridgeRegistry, TelegramPoller

logger = logging.getLogger(__name__)

SURFACE_NAME = "telegram"


#: Mounted while the app is built (see the `Surface` port on why routes are
#: constants). The console's Telegram page ships with the surface that can
#: answer it, not with the console.
ROUTERS = (telegram_admin_router,)
STATIC_FILES: tuple[StaticFile, ...] = ()


@dataclass(frozen=True, slots=True)
class TelegramSurfaceConfig:
    admin_tool: Tool | None = None
    polls: bool = True


DEFAULT_SURFACE_CONFIG = TelegramSurfaceConfig()


class TelegramSurface:
    """The Telegram bot, plugged into the service (`Surface`)."""

    def __init__(
        self,
        registry: TelegramBridgeRegistry,
        poller: TelegramPoller,
        config: TelegramSurfaceConfig = DEFAULT_SURFACE_CONFIG,
    ) -> None:
        self._registry = registry
        self._poller = poller
        self._admin_tool = config.admin_tool
        # A token may be long-polled by exactly one process. With a separate
        # ingestion node this pod renders and does not read — two readers
        # would steal each other's updates.
        self._polls = config.polls
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
        """Poll, if this process is the one that reads.

        Nothing is prepared per dialog here, and that is deliberate. Startup
        used to build a bridge for every known Telegram dialog so a scheduled
        run would have somewhere to land — but a bridge cannot be built
        without its dialog's actor, and building that actor is what CLAIMS
        the dialog. Every pod therefore took every dialog on every start, and
        the last one to boot owned them all: each deploy cut off whatever its
        peer was mid-answer on.

        Nothing was gained for it. `ConversationManager` attaches this
        surface to each actor it builds, and it builds one on every path that
        can produce an answer — an arriving message, a cron firing, a settled
        collection — so the bridge exists exactly when there is something to
        deliver through it.
        """
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())
        self._task.add_done_callback(_report_failure)

    async def _run(self) -> None:
        if not self._polls:
            logger.info("telegram: rendering only, ingestion runs elsewhere")
            return
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
