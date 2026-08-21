"""Collection storage and structured-response handling for the deployment."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from octoforge_core.composition import (
    CollectionsRuntime,
    ResponseLayer,
    build_collections,
    build_response_layer,
)
from octoforge_core.net.collections.api import CollectionConfig
from octoforge_core.net.response_memory import MEGABYTE, ResponseMemoryConfig
from octoforge_server.config import Settings

from octoforge_deploy.runtime_database import DatabaseRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResponseRuntime:
    collections: CollectionsRuntime | None
    responses: ResponseLayer


def build_response_runtime(settings: Settings, database: DatabaseRuntime) -> ResponseRuntime:
    collections_config = _collections_config(settings)
    collections = build_collections(database.engine, database.sessions, collections_config)
    responses = build_response_layer(
        collections,
        collections_config,
        _memory_config(settings),
    )
    return ResponseRuntime(collections, responses)


def start_collection_sweep(
    runtime: ResponseRuntime,
    interval_seconds: float,
) -> tuple[asyncio.Task[None], ...]:
    if runtime.collections is None:
        return ()
    return (asyncio.create_task(_sweep_collections(runtime.collections, interval_seconds)),)


async def _sweep_collections(
    collections: CollectionsRuntime,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            dropped = await collections.store.delete_expired()
            if dropped:
                logger.info("expired collections removed", extra={"count": dropped})
        except Exception:
            logger.exception("collection expiry sweep failed")


def _collections_config(settings: Settings) -> CollectionConfig:
    return CollectionConfig(
        ttl_seconds=settings.collections_ttl_seconds,
        max_per_user=settings.collections_max_per_user,
        max_bytes_per_user=settings.collections_max_mb_per_user * MEGABYTE,
        query_max_limit=settings.collections_query_max_limit,
        inline_max_chars=settings.collections_inline_max_chars,
        collect_max_pages=settings.collections_collect_max_pages,
        query_default_chars=settings.collections_query_default_chars,
        query_max_chars=settings.collections_query_max_chars,
    )


def _memory_config(settings: Settings) -> ResponseMemoryConfig:
    return ResponseMemoryConfig(
        max_response_chars=settings.response_memory_max_mb * MEGABYTE,
        budget_chars=settings.response_memory_budget_mb * MEGABYTE,
        get_default_chars=settings.response_get_default_chars,
        get_max_chars=settings.response_get_max_chars,
    )
