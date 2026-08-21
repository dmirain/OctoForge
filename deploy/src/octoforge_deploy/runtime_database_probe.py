"""Best-effort database bootstrap, capability probing, and retention."""

from __future__ import annotations

import logging

from octoforge_core import bootstrap_schema, init_db
from octoforge_core.composition import LexicalBackend
from octoforge_core.db.search_extensions import (
    PG_TEXTSEARCH,
    installed_search_extensions,
)
from octoforge_core.db.sqlite_fts import has_sqlite_fts
from octoforge_core.retention_sweep import RetentionSweeper
from octoforge_server.config import Settings
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

logger = logging.getLogger("octoforge_deploy.main")


async def bootstrap(engine: AsyncEngine) -> None:
    """Migrate to head, falling back to a create-all schema."""
    try:
        await bootstrap_schema(engine)
    except Exception:
        logger.warning("Alembic migration failed; falling back to create_all", exc_info=True)
        await init_db(engine)


async def probe_backends(engine: AsyncEngine) -> tuple[frozenset[str], LexicalBackend]:
    extensions = await _probe_extensions(engine)
    return extensions, await _probe_lexical(engine, extensions)


async def sweep_retention(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    policy = settings.retention_policy()
    if not policy.enabled():
        return
    try:
        outcome = await RetentionSweeper(sessions, policy).sweep()
    except SQLAlchemyError:
        logger.warning("retention sweep failed; starting without it", exc_info=True)
        return
    logger.info("retention (%s): removed %s", policy.describe(), outcome.describe())


async def _probe_extensions(engine: AsyncEngine) -> frozenset[str]:
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(installed_search_extensions)
    except SQLAlchemyError:
        logger.warning("could not probe search extensions; assuming none", exc_info=True)
        return frozenset()


async def _probe_lexical(
    engine: AsyncEngine,
    extensions: frozenset[str],
) -> LexicalBackend:
    if PG_TEXTSEARCH in extensions:
        return LexicalBackend.POSTGRES
    try:
        async with engine.connect() as connection:
            if await connection.run_sync(has_sqlite_fts):
                return LexicalBackend.SQLITE
    except SQLAlchemyError:
        logger.warning("could not probe SQLite full-text search; assuming none", exc_info=True)
    return LexicalBackend.NONE
