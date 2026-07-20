"""Async engine and session factories plus schema bootstrap."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from octoforge_core.db.base import Base

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def create_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine for the given database URL."""
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables directly via create_all.

    Used by tests and as the composition-root fallback when Alembic migrations
    cannot run; production startup prefers `bootstrap_schema`.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def bootstrap_schema(engine: AsyncEngine) -> None:
    """Bring the schema to the latest Alembic revision.

    A fresh database has every table created by the baseline migration; a
    database that predates Alembic (tables but no `alembic_version`) is stamped
    at head (its schema is assumed current); an already-managed database is
    upgraded. The transaction is committed explicitly: Alembic leaves it open
    (the caller owns the connection), and closing an async connection would
    roll it back, losing the version row and ALTERs.
    """
    async with engine.connect() as connection:
        await connection.run_sync(_bootstrap_sync)
        await connection.commit()


def _bootstrap_sync(connection: Connection) -> None:
    tables = set(inspect(connection).get_table_names())
    config = _alembic_config(connection)
    if tables and "alembic_version" not in tables:
        command.stamp(config, "head")  # adopt a pre-Alembic database at baseline
    else:
        command.upgrade(config, "head")  # fresh or already-managed database


def _alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config
