"""The browser chat UI as an installed surface.

It is a page plus the dialog API it calls, and nothing else — which is why a
deployment driven only from Telegram can leave it out entirely. The dialog
API itself belongs to the service: it is what every surface talks to, not
something this one owns.
"""

from collections.abc import Sequence
from pathlib import Path

from fastapi import APIRouter
from octoforge_core.agent.runner import DialogSurface
from octoforge_core.tools.base import Tool

from octoforge_web.channels import WEB_CHANNEL
from octoforge_web.surfaces import StaticFile

SURFACE_NAME = "webui"
STATIC_DIR = Path(__file__).parent / "static"


_PAGE = STATIC_DIR / "index.html"

#: `/` as well as `/index.html`: that is where a browser arrives.
ROUTERS: tuple[APIRouter, ...] = ()
STATIC_FILES = (
    StaticFile(path="/", file=_PAGE),
    StaticFile(path="/index.html", file=_PAGE),
)


class WebUiSurface:
    """The chat page (`Surface`)."""

    @property
    def name(self) -> str:
        return SURFACE_NAME

    def channel(self) -> str | None:
        """A browser talks to the service directly."""
        return WEB_CHANNEL

    def dialog_surface(self) -> DialogSurface | None:
        """None: a browser subscribes over the API rather than being pushed to."""
        return None

    def tools(self) -> Sequence[Tool]:
        return ()

    async def start(self) -> None:
        """Nothing to run."""

    async def aclose(self) -> None:
        """Nothing to stop."""
