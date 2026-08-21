"""Deployment-owned background loops."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core import (
    ConversationManager,
    build_collecting_sweeper,
    build_cron_scheduler,
)
from octoforge_core.agent.collecting import CollectingSources, CollectingSweepConfig
from octoforge_core.cron.scheduler import CronSchedulerConfig, CronSchedulerTiming
from octoforge_server.config import Settings

from octoforge_deploy.runtime_database import DatabaseRuntime
from octoforge_deploy.runtime_mcp import McpRuntime, start_mcp_sync
from octoforge_deploy.runtime_responses import ResponseRuntime, start_collection_sweep


@dataclass(frozen=True, slots=True)
class BackgroundRequest:
    settings: Settings
    database: DatabaseRuntime
    manager: ConversationManager
    mcp: McpRuntime
    responses: ResponseRuntime


def start_background(request: BackgroundRequest) -> tuple[asyncio.Task[None], ...]:
    return (
        _start_cron(request),
        _start_collecting(request),
        start_mcp_sync(request.mcp, request.settings),
        *start_collection_sweep(
            request.responses,
            request.settings.collections_sweep_interval_seconds,
        ),
    )


async def stop_background(tasks: tuple[asyncio.Task[None], ...]) -> None:
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def _start_cron(request: BackgroundRequest) -> asyncio.Task[None]:
    settings = request.settings
    scheduler = build_cron_scheduler(
        request.database.stores.cron,
        request.manager,
        CronSchedulerConfig(
            owner=uuid.uuid4().hex,
            timing=CronSchedulerTiming(
                poll_interval_seconds=settings.cron_poll_interval_seconds,
                lease_ttl_seconds=settings.cron_lease_ttl_seconds,
                replay_limit=settings.cron_replay_limit,
            ),
        ),
    )
    return asyncio.create_task(scheduler.run_forever())


def _start_collecting(request: BackgroundRequest) -> asyncio.Task[None]:
    settings = request.settings
    manager_stores = request.database.stores.manager
    sweeper = build_collecting_sweeper(
        CollectingSources(
            exchanges=manager_stores.exchanges,
            dialogs=manager_stores.dialogs,
        ),
        request.manager,
        CollectingSweepConfig(
            quiet_seconds=settings.material_quiet_seconds,
            interval_seconds=settings.material_sweep_interval_seconds,
        ),
    )
    return asyncio.create_task(sweeper.run_forever())
