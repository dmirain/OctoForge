"""Surfaces: what an installed interface contributes, and what it cannot reach.

The point of the port is that removing an interface is a matter of not
constructing it. These tests assert the two halves of that: the service and
the other surfaces do not reach into one, and one that misbehaves does not
take the rest down.
"""

import ast
import logging
from collections.abc import Sequence
from pathlib import Path

import pytest
from octoforge_console import surface as console
from octoforge_core.agent.runner import DialogSurface
from octoforge_core.tools.base import Tool
from octoforge_core.tools.registry import ToolRegistry
from octoforge_server.surfaces import Surface
from octoforge_telegram import surface as telegram_surface
from octoforge_webui import surface as webui

from octoforge_deploy import main

REPO = Path(__file__).resolve().parents[2]
SERVICE_SRC = REPO / "server" / "src" / "octoforge_server"
SURFACE_SRC = {
    "telegram": REPO / "surfaces" / "telegram" / "src" / "octoforge_telegram",
    "console": REPO / "surfaces" / "console" / "src" / "octoforge_console",
    "webui": REPO / "surfaces" / "webui" / "src" / "octoforge_webui",
}
DEPLOY_SRC = REPO / "deploy" / "src" / "octoforge_deploy"
TELEGRAM_ROUTE = "/api/admin/telegram/users"


PREFIX = "octoforge_"


def imported_modules(path: Path) -> set[str]:
    """Every `octoforge_*` module a file imports."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(PREFIX):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith(PREFIX))
    return found


#: Distributions, by the package each one installs.
SERVICE = "octoforge_server"
SURFACES = ("octoforge_telegram", "octoforge_console", "octoforge_webui")
DEPLOYMENT = "octoforge_deploy"


def package_imports(directory: Path) -> set[str]:
    """Every `octoforge_*` module imported anywhere under a package."""
    found: set[str] = set()
    for file in directory.rglob("*.py"):
        found |= imported_modules(file)
    return found


def test_the_service_imports_no_surface() -> None:
    """If it did, "optional" would be a fiction: removing an interface would
    mean editing the service. This is the rule the whole layout exists for."""
    imports = package_imports(SERVICE_SRC)

    assert not [name for name in imports if name.startswith((*SURFACES, DEPLOYMENT))]


@pytest.mark.parametrize("surface", sorted(SURFACE_SRC))
def test_a_surface_imports_no_other_surface(surface: str) -> None:
    """Each one has to be removable on its own."""
    others = tuple(name for name in SURFACES if not name.endswith(surface))
    imports = package_imports(SURFACE_SRC[surface])

    assert not [name for name in imports if name.startswith(others)]


@pytest.mark.parametrize("surface", sorted(SURFACE_SRC))
def test_a_surface_does_not_reach_into_the_deployment(surface: str) -> None:
    """A surface that assembled the deployment would decide what runs beside
    it — which is the deployment's job, and the reason the standalone Telegram
    entry point lives there rather than here."""
    imports = package_imports(SURFACE_SRC[surface])

    assert not [name for name in imports if name.startswith(DEPLOYMENT)]


def test_only_the_deployment_knows_every_interface() -> None:
    imports = package_imports(DEPLOY_SRC)

    assert all(any(name.startswith(surface) for name in imports) for surface in SURFACES)


def test_the_console_does_not_reach_into_telegram() -> None:
    """It used to serve Telegram's who-is-who by reading that surface's store,
    which quietly made the console undeployable without a bot."""
    imports = package_imports(SURFACE_SRC["console"])

    assert not [name for name in imports if name.startswith("octoforge_telegram")]


def test_the_telegram_page_is_served_by_telegram() -> None:
    """The route did not disappear with the coupling — it moved to the surface
    that can answer it."""
    paths = {route.path for router in telegram_surface.ROUTERS for route in router.routes}

    assert TELEGRAM_ROUTE in paths


def test_the_console_and_the_chat_page_keep_their_urls() -> None:
    """Moving them into their own distributions must not move them in a browser."""
    assert [item.path for item in console.STATIC_FILES] == ["/admin.html"]
    assert [item.path for item in webui.STATIC_FILES] == ["/", "/index.html"]


def test_a_deployment_without_telegram_still_has_its_interfaces() -> None:
    surfaces = main._installed_surfaces(None)

    assert [surface.name for surface in surfaces] == ["console", "webui"]


class BrokenSurface:
    """A surface that fails at everything it is asked to do."""

    def __init__(self) -> None:
        self.closed = False

    @property
    def name(self) -> str:
        return "broken"

    def dialog_surface(self) -> DialogSurface | None:
        return None

    def tools(self) -> Sequence[Tool]:
        return ()

    async def start(self) -> None:
        raise RuntimeError("no")

    async def aclose(self) -> None:
        self.closed = True
        raise RuntimeError("still no")


class QuietSurface:
    """A surface that works, standing next to one that does not."""

    def __init__(self) -> None:
        self.started = False

    @property
    def name(self) -> str:
        return "quiet"

    def dialog_surface(self) -> DialogSurface | None:
        return None

    def tools(self) -> Sequence[Tool]:
        return ()

    async def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        pass


async def test_a_surface_that_cannot_start_does_not_stop_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken bot must not cost the console and the API."""
    broken, quiet = BrokenSurface(), QuietSurface()
    surfaces: tuple[Surface, ...] = (broken, quiet)

    with caplog.at_level(logging.ERROR):
        await main._start_surfaces(main._attach_renderers(surfaces, _NoopManager(), ToolRegistry()))

    assert quiet.started
    assert any("broken" in record.message for record in caplog.records)


async def test_a_surface_that_cannot_close_does_not_block_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    broken = BrokenSurface()

    with caplog.at_level(logging.ERROR):
        await main._close_surface(broken)

    assert broken.closed


class _NoopManager:
    """Just enough of the manager for `_install` to talk to."""

    def use_surface(self, surface: DialogSurface) -> None:
        raise AssertionError("no surface here declares a renderer")
