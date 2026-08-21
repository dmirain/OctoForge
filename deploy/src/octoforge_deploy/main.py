"""FastAPI application factory and stable deployment entry points."""

from __future__ import annotations

from fastapi import FastAPI
from octoforge_server.app import build_app
from octoforge_server.config import Settings
from octoforge_server.logs import LoggingConfig, configure_logging

from octoforge_deploy.runtime_entry import runtime
from octoforge_deploy.runtime_http import build_reranker as _runtime_build_reranker
from octoforge_deploy.runtime_surfaces import attach_renderers as _runtime_attach_renderers
from octoforge_deploy.runtime_surfaces import close_surface as _runtime_close_surface
from octoforge_deploy.runtime_surfaces import installed_surfaces as _runtime_installed_surfaces
from octoforge_deploy.runtime_surfaces import start_surfaces as _runtime_start_surfaces
from octoforge_deploy.runtime_surfaces import surface_routes as _surface_routes

WEB_PROCESS_LOG_NAME = "app"

_attach_renderers = _runtime_attach_renderers
_build_reranker = _runtime_build_reranker
_close_surface = _runtime_close_surface
_installed_surfaces = _runtime_installed_surfaces
_start_surfaces = _runtime_start_surfaces


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build this deployment: the service and its installed interfaces."""
    resolved = settings or Settings()
    configure_logging(
        LoggingConfig(
            WEB_PROCESS_LOG_NAME,
            log_dir=resolved.log_dir,
            max_mb=resolved.log_max_mb,
            backups=resolved.log_backups,
        ),
    )
    return build_app(resolved, runtime, _surface_routes())


app = create_app()

__all__ = ["app", "create_app", "runtime"]
