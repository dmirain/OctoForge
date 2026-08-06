"""Async engine and session factories plus schema bootstrap."""

import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event, inspect
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, ConnectionPoolEntry

# Imported for their side effect: every model module has to register its tables
# on `Base.metadata` before `create_all` runs (same list as `migrations/env.py`).
# The model modules only depend on `db.base`, so this cannot cycle back here.
# Ruff quirk: all these bind one root name, so F401 fires only on the LAST
# import — the single noqa below covers the block. An autofix once deleted
# the whole cascade (caught by the Postgres bootstrap test): if you touch
# this list, rerun `make test-pg`.
import octoforge_core.context.models
import octoforge_core.cron.models
import octoforge_core.datasets.models
import octoforge_core.dialogs.models
import octoforge_core.identity.models
import octoforge_core.instructions.models
import octoforge_core.mcp.models
import octoforge_core.params.models
import octoforge_core.secrets.models
import octoforge_core.settings.models
import octoforge_core.tariffs.models
import octoforge_core.tasks.models  # noqa: F401
from octoforge_core.db.base import Base
from octoforge_core.db.search_extensions import ensure_bm25_indexes, ensure_search_extensions

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_BASELINE_REVISION = "675056c8fffd"
SQLITE_DIALECT = "sqlite"
# How long a blocked SQLite writer waits for the lock before giving up. Long
# enough to ride out an ordinary concurrent commit, short enough that a genuine
# deadlock still surfaces rather than hanging a dialog forever.
SQLITE_BUSY_TIMEOUT_MS = 5000


def create_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine for the given database URL.

    An in-memory SQLite database exists per CONNECTION: with the default
    pool, a second pooled connection silently gets its own empty database and
    concurrent writers (the dialog actor and its process pumps) lose updates
    to a ghost. `:memory:` is therefore mapped to a unique named shared-cache
    database — every session gets its own connection, all connections see the
    same data, and SQLite does its own locking.

    Two follow-on gotchas, both found by a live 1-in-3 flake of
    `test_dialog_is_get_or_created_per_user` (2026-07-28) and reproduced in
    isolation before being fixed here:

    - SQLAlchemy's aiosqlite dialect auto-selects `StaticPool` (one physical
      connection reused by every session) whenever the URL's `mode=memory`
      query param is present — `cache=shared` does not change that heuristic.
      Pinning one shared connection is worse than the ghost-DB problem it
      replaces: an unrelated session's implicit rollback-on-close (e.g. a
      plain SELECT whose `async with session_factory()` block never calls
      `commit()`) runs on the *same* physical connection and silently wipes
      another session's just-inserted, not-yet-committed row — reproduced
      directly (a concurrent read-only session closing mid-write made a
      committed INSERT vanish) and confirmed live: an exchange settling to
      ANSWERED logged as done, and a whole user message, both never landed in
      the database. `poolclass=AsyncAdaptedQueuePool` is forced explicitly so
      every session gets its own connection instead (same class a real file
      URL would get).
    - Real per-session connections then hit the other side of the trade-off:
      SQLite's shared-cache mode raises `OperationalError: database table is
      locked` (SQLITE_LOCKED) the moment two connections touch the same
      table while one has an uncommitted write — and, unlike SQLITE_BUSY,
      confirmed NOT to honor `busy_timeout` (a concurrent writer to an
      unrelated table failed immediately instead of waiting for it to
      commit). SQLite fundamentally allows one writer at a time anyway, so
      `pool_size=1, max_overflow=0` makes that explicit: every session in
      this process gets the one connection in turn (`AsyncAdaptedQueuePool`
      blocks a second checkout instead of opening another), which serializes
      the rare true overlap without ever deadlocking — every store method
      here opens and fully closes its own session before returning, so no
      code path holds one checkout while awaiting a second. The pool still
      keeps that one connection open between checkouts, so the shared-cache
      database outlives individual sessions until `engine.dispose()`.
      `PRAGMA read_uncommitted=1` is kept as a second line of defense (it
      alone was enough for the read/write case, just not writer/writer).

    Tests only; file/Postgres URLs pass through untouched.
    """
    if ":memory:" in database_url:
        name = f"mem_{uuid.uuid4().hex}"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true",
            poolclass=AsyncAdaptedQueuePool,
            pool_size=1,
            max_overflow=0,
        )
        event.listen(engine.sync_engine, "connect", _enable_read_uncommitted)
        return engine
    engine = create_async_engine(database_url)
    if engine.dialect.name == SQLITE_DIALECT:
        event.listen(engine.sync_engine, "connect", _enable_wal)
    return engine


def _enable_wal(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    """Put a SQLite FILE database into WAL mode with a write timeout.

    The default rollback journal takes an exclusive lock for the whole of every
    write, so a reader arriving mid-write gets `database is locked` immediately
    rather than waiting. One process serves every dialog here, and the actor,
    the cron scheduler and the recovery sweeps all touch the database at once,
    so that is not a rare interleaving.

    WAL lets readers proceed against the last committed state while a write is
    in flight, and `busy_timeout` makes the remaining writer/writer overlap wait
    instead of failing. `synchronous=NORMAL` is the standard WAL pairing: a
    crash can lose the last transactions but never corrupts the database, and
    the alternative costs an fsync per commit.

    In-memory databases are excluded (they take the shared-cache branch above,
    where WAL does not apply).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _enable_read_uncommitted(
    dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry
) -> None:
    """Relax shared-cache table locking so concurrent sessions don't SQLITE_LOCKED.

    Fires on every new pooled connection (`connect`, not `checkout`): the
    pragma is per-connection state, so it must be reapplied whenever
    `AsyncAdaptedQueuePool` opens a fresh one.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA read_uncommitted=1")
    finally:
        cursor.close()


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

    Skipping the chain also skips whatever the chain does beyond tables, so the
    optional search extensions are created here explicitly. Without this a fresh
    Postgres deployment would be stamped at head having never run
    `a2f7c5e9d148`, and would silently lack pgvector and BM25 forever.

    They are created BEFORE the tables, not after: `instructions` declares a
    `VECTOR` column, and Postgres cannot create a column of a type no extension
    has defined yet ("type vector does not exist"). In the migration chain the
    same ordering falls out of the revision order.
    """
    ensure_search_extensions(connection)
    Base.metadata.create_all(connection)
    # after the tables: a BM25 index needs the table it indexes, and this path
    # never runs c7e2a91f4d38, which builds them for a database that exists
    ensure_bm25_indexes(connection)
    command.stamp(config, "head")


def _alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    return config
