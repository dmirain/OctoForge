"""What an interface installed on top of the service contributes to it.

The service serves core over HTTP and nothing else. Everything a human or a
chat talks to — the Telegram bot, the operator console, the chat UI — is a
*surface*: an optional thing that plugs in and declares what it adds.

That word is deliberate about the direction of the arrow. The service must
never import a surface; if it did, "optional" would be a fiction and removing
one would mean editing the service. Only the composition root above both is
allowed to know which surfaces exist, which is what makes a deployment
without Telegram, or with Slack instead, a matter of what is installed rather
than of what is written.

A surface may plug in two ways, and the port is the same either way: as a
library this process loads, or as its own process talking to the service over
HTTP. Which one a deployment uses is a deployment decision — see
`docs/architecture.md`, and `docs/reference/configuration.md` for the
Telegram case, where the second mode exists because one bot token may be
long-polled by exactly one process.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter
from octoforge_core.agent.runner import DialogSurface
from octoforge_core.tools.base import Tool


@dataclass(frozen=True, slots=True)
class StaticFile:
    """One file a surface serves, and the exact URL it answers on.

    Per file rather than per directory because several surfaces serve from the
    same root: two directories cannot both be mounted at `/`, and the console
    has lived at `/admin.html` long enough that moving it would be a breaking
    change dressed up as a refactor.
    """

    path: str
    file: Path


@dataclass(frozen=True, slots=True)
class SurfaceRoutes:
    """What a surface serves, gathered before the surface itself exists.

    Mounting happens while the application is built; a surface needs the
    database and the model client, which are assembled later in the lifespan.
    So routes travel separately, as module constants collected by the
    composition root.
    """

    name: str
    routers: Sequence[APIRouter] = ()
    static_files: Sequence[StaticFile] = ()


class Surface(Protocol):
    """An interface plugged into the service.

    Every method answers "what does this add", and every one of them may
    answer "nothing" — a surface that only renders a chat has no tools, and
    one that only serves a page has no dialog renderer.

    Routes and pages are NOT here. They are mounted while the application is
    built, which is before any of this exists — a surface needs the database
    and the model client, and those are assembled in the lifespan. So each
    surface module exposes its routers and pages as constants (`ROUTERS`,
    `STATIC_FILES`) that the composition root mounts directly, and this port
    covers what a surface *does* rather than what it serves.
    """

    @property
    def name(self) -> str:
        """Short name for the startup report and logs."""
        ...

    def channel(self) -> str | None:
        """The channel whose dialogs belong to this surface, if it has one.

        What the service accepts in `X-Channel` is the union of these — the
        surfaces installed decide it, not a list written into the service.
        """
        ...

    def dialog_surface(self) -> DialogSurface | None:
        """What renders a dialog of this surface's channel, if it has one."""
        ...

    def tools(self) -> Sequence[Tool]:
        """Tools this surface gives the agent (an admin bot command, say)."""
        ...

    async def start(self) -> None:
        """Begin background work: polling, watching, whatever this surface does."""
        ...

    async def aclose(self) -> None:
        """Stop it again; the application is shutting down."""
        ...
