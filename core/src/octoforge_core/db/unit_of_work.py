"""One transaction spanning several store calls without changing their ports."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db._unit_context import (
    active_unit,
    borrow_session,
    in_unit_of_work,
    mark_write,
    outside_uow,
    unit_has_writes,
)

__all__ = [
    "UnitOfWork",
    "in_unit_of_work",
    "outside_uow",
    "read_session",
    "unit_has_writes",
    "write_session",
]


class UnitOfWork:
    """Open one shared transaction for store calls in an async block."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        if self._session_factory is None:
            yield
            return
        if in_unit_of_work():
            raise RuntimeError(
                "a unit of work is already active; nesting would merge two transactions"
            )
        async with self._session_factory() as session:
            with active_unit(session):
                yield
                await session.commit()


@asynccontextmanager
async def read_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Borrow the unit session or open a fresh read-only session."""
    async with borrow_session(session_factory) as (session, _owned):
        yield session


@asynccontextmanager
async def write_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Borrow the unit session or commit a fresh write session at exit."""
    async with borrow_session(session_factory) as (session, owned):
        yield session
        if owned:
            await session.commit()
        else:
            mark_write(session)
