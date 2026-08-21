"""Ordered startup and shutdown of an assembled graph."""

from __future__ import annotations

from octoforge_deploy.runtime_background import (
    BackgroundRequest,
    start_background,
    stop_background,
)
from octoforge_deploy.runtime_state import RunningRuntime, RuntimeGraph
from octoforge_deploy.runtime_surfaces import close_surface, start_surfaces


async def start_runtime(graph: RuntimeGraph) -> RunningRuntime:
    # Renderers are attached during graph construction, before recovery builds actors.
    await graph.manager.recover_interrupted()
    graph.manager.start()
    background = start_background(
        BackgroundRequest(
            graph.settings,
            graph.database,
            graph.manager,
            graph.mcp,
            graph.responses,
        ),
    )
    await start_surfaces(graph.surfaces)
    return RunningRuntime(graph, background)


async def stop_runtime(running: RunningRuntime) -> None:
    await stop_background(running.background)
    for surface in running.graph.surfaces:
        await close_surface(surface)
    await running.graph.manager.stop_all()
