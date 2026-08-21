"""Async engine/session factories and SQLite concurrency policy."""

import uuid

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool, ConnectionPoolEntry

SQLITE_DIALECT = "sqlite"
SQLITE_BUSY_TIMEOUT_MS = 5000


def create_engine(database_url: str) -> AsyncEngine:
    """Create an async engine, serializing in-memory SQLite through one connection."""
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


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _enable_wal(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def _enable_read_uncommitted(
    dbapi_connection: DBAPIConnection,
    _record: ConnectionPoolEntry,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA read_uncommitted=1")
    finally:
        cursor.close()
