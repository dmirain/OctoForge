"""Async engine and session factories plus schema bootstrap."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from octoforge_core.db.base import Base


def create_engine(database_url: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine for the given database URL."""
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create all tables (create_all; Alembic arrives with the first destructive migration)."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
