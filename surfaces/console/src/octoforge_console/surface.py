"""The operator console as an installed surface.

A console is one interface among several, not a privileged part of the
service: it reads and edits the same data a Telegram admin edits through
`admin_manage`. Treating it as a surface is what lets a deployment ship
without it — a headless installation driven entirely from chat is a
legitimate shape, and today it would still carry the console's routes and
page.

The pages of *other* surfaces are theirs, not the console's: the Telegram
who-is-who route ships with Telegram, because Telegram is what can answer it.
"""

from collections.abc import Sequence
from pathlib import Path

from octoforge_core.agent.runner import DialogSurface
from octoforge_core.tools.base import Tool
from octoforge_server.surfaces import StaticFile

from octoforge_console.routes import router as admin_router

SURFACE_NAME = "console"
STATIC_DIR = Path(__file__).parent / "static"


#: Mounted while the app is built, before any surface object exists.
ROUTERS = (admin_router,)
STATIC_FILES = (StaticFile(path="/admin.html", file=STATIC_DIR / "admin.html"),)


class ConsoleSurface:
    """The operator console (`Surface`)."""

    @property
    def name(self) -> str:
        return SURFACE_NAME

    def channel(self) -> str | None:
        """None: the console reads every dialog and owns no channel."""
        return None

    def dialog_surface(self) -> DialogSurface | None:
        """None: the console reads dialogs, it is not one."""
        return None

    def tools(self) -> Sequence[Tool]:
        return ()

    async def start(self) -> None:
        """Nothing to run: the console is served, not polled."""

    async def aclose(self) -> None:
        """Nothing to stop."""
