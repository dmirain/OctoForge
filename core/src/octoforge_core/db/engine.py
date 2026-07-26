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

# Imported for their side effect: every model module has to register its tables
# on `Base.metadata` before `create_all` runs (same list as `migrations/env.py`).
# The model modules only depend on `db.base`, so this cannot cycle back here.
import octoforge_core.context.models
import octoforge_core.cron.models
import octoforge_core.datasets.models
import octoforge_core.db.models
import octoforge_core.instructions.models
import octoforge_core.secrets.models  # noqa: F401
from octoforge_core.db.base import Base

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_BASELINE_REVISION = "675056c8fffd"
SQLITE_DIALECT = "sqlite"


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

    On SQLite a fresh database has every table created by the baseline
    migration; a database that predates Alembic (tables but no
    `alembic_version`) is stamped at the baseline revision (its schema is
    assumed to match `create_all`, which is what created it) and then upgraded,
    so later ALTER migrations still apply; an already-managed database is
    upgraded. A fresh database on another dialect skips the historical chain —
    see `_create_and_stamp`. The transaction is committed explicitly: Alembic
    leaves it open (the caller owns the connection), and closing an async
    connection would roll it back, losing the version row and ALTERs.
    """
    async with engine.connect() as connection:
        await connection.run_sync(_bootstrap_sync)
        await connection.commit()


def _bootstrap_sync(connection: Connection) -> None:
    tables = set(inspect(connection).get_table_names())
    config = _alembic_config(connection)
    if not tables and connection.dialect.name != SQLITE_DIALECT:
        _create_and_stamp(connection, config)
        return
    if tables and "alembic_version" not in tables:
        # Adopt a pre-Alembic database. One created by an old octoforge (or an
        # old create_all) still has the memories table: stamp it at baseline so
        # the whole chain — including the memories→instructions data migration —
        # replays over it. One created by today's create_all already matches
        # head (create_all builds the current metadata, and memories is gone
        # from it): stamp head, or the replay would touch dropped tables.
        adopted = _BASELINE_REVISION if "memories" in tables else "head"
        command.stamp(config, adopted)
    command.upgrade(config, "head")  # fresh, legacy, or already-managed database


def _create_and_stamp(connection: Connection, config: Config) -> None:
    """Create the current schema from the models and stamp it at head.

    The historical chain cannot be replayed outside SQLite: three migrations
    declare boolean columns with `server_default=sa.text('0')` (Postgres
    rejects an integer default for a boolean), one creates a partial index with
    `sqlite_where` only, and another builds indexes from raw SQL. Migrations are
    append-only (a `PreToolUse` hook guards committed ones), so they are not
    retrofitted; instead a fresh non-SQLite database gets today's schema
    directly and is stamped at head, after which later migrations apply
    normally — those must be written dialect-neutrally (see AGENTS.md).
    """
    Base.metadata.create_all(connection)
    command.stamp(config, "head")


def _alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config
