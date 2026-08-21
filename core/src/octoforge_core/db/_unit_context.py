"""Context-local session ownership and concurrency guard for units of work."""

from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

T = TypeVar("T")

ACTIVE_SESSION: ContextVar[AsyncSession | None] = ContextVar("uow_session", default=None)
BUSY_KEY = "uow_call_in_flight"
OPEN_KEY = "uow_open"
WROTE_KEY = "uow_has_writes"
CONCURRENT_USE = (
    "concurrent store calls inside a unit of work share one connection; "
    "run parallel reads outside the unit, each wrapped in outside_uow(...)"
)


@contextmanager
def active_unit(session: AsyncSession) -> Iterator[None]:
    session.info[OPEN_KEY] = True
    session.info[WROTE_KEY] = False
    token = ACTIVE_SESSION.set(session)
    try:
        yield
    finally:
        session.info[OPEN_KEY] = False
        ACTIVE_SESSION.reset(token)


async def outside_uow(coro: Coroutine[Any, Any, T]) -> T:
    """Run one store call outside the active unit, on its own session."""
    token = ACTIVE_SESSION.set(None)
    try:
        return await coro
    finally:
        ACTIVE_SESSION.reset(token)


def unit_has_writes(session: AsyncSession) -> bool:
    """Whether this active unit already completed a write call."""
    return bool(session.info.get(WROTE_KEY))


def mark_write(session: AsyncSession) -> None:
    session.info[WROTE_KEY] = True


def in_unit_of_work() -> bool:
    """Whether a live unit is active in the current task context."""
    active = ACTIVE_SESSION.get()
    return active is not None and bool(active.info.get(OPEN_KEY))


@asynccontextmanager
async def borrow_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AsyncSession, bool]]:
    """Yield the unit session unowned, or a fresh session owned by the caller."""
    active = ACTIVE_SESSION.get()
    if active is None or not active.info.get(OPEN_KEY):
        async with session_factory() as session:
            yield session, True
        return
    if active.info.get(BUSY_KEY):
        raise RuntimeError(CONCURRENT_USE)
    active.info[BUSY_KEY] = True
    try:
        yield active, False
    finally:
        active.info[BUSY_KEY] = False
