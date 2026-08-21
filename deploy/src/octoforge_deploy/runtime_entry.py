"""Shared composition-root context for HTTP and standalone surfaces."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from octoforge_server.capabilities import CapabilityReport, log_capabilities
from octoforge_server.config import Settings
from octoforge_server.runtime_state import Runtime
from octoforge_telegram import capabilities as telegram_capabilities
from octoforge_telegram.config import TelegramSettings

from octoforge_deploy.runtime_assembly import GraphBuildRequest, build_runtime_graph
from octoforge_deploy.runtime_database import DatabaseRuntime, build_database_runtime
from octoforge_deploy.runtime_http import open_http_clients
from octoforge_deploy.runtime_lifecycle import start_runtime, stop_runtime
from octoforge_deploy.runtime_result import build_runtime_result

logger = logging.getLogger("octoforge_deploy.main")


@asynccontextmanager
async def runtime(settings: Settings) -> AsyncIterator[Runtime]:
    """Build all services and background work without opening an HTTP listener."""
    telegram = TelegramSettings()
    database = await build_database_runtime(settings, telegram)
    _report_capabilities(settings, telegram, database)
    try:
        async with open_http_clients(settings) as clients:
            graph = await build_runtime_graph(
                GraphBuildRequest(settings, telegram, database),
                clients,
            )
            running = await start_runtime(graph)
            try:
                yield build_runtime_result(graph)
            finally:
                await stop_runtime(running)
    finally:
        await database.aclose()


def _report_capabilities(
    settings: Settings,
    telegram: TelegramSettings,
    database: DatabaseRuntime,
) -> None:
    log_capabilities(
        settings,
        logger,
        CapabilityReport(
            database.extensions,
            database.lexical,
            (telegram_capabilities.describe(telegram),),
        ),
    )
