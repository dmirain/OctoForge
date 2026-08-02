"""Unit of work: one transaction spanning several store calls, without touching ports.

Every store method historically opened its own session and committed it — a
transaction per call, which is three network round trips (BEGIN, the statement,
COMMIT) per store call when the database is remote. A unit of work groups the
calls of one phase into a single transaction: the runner opens it, the store
calls that run inside it share its session, and the commit happens once at the
end. No store signature changes; the current session travels in a `ContextVar`.

The mechanism deliberately trades magic for three explicit rules:

- **A unit of work is for writes; parallel reads run outside it.** The shared
  session is a single database connection, and concurrent statements on one
  connection are not a thing. `read_session`/`write_session` refuse concurrent
  use of the active unit's session with a `RuntimeError` naming `outside_uow`,
  so a stray `asyncio.gather` inside a unit fails loudly in tests instead of
  corrupting a transaction in production. To run reads concurrently while a
  unit is active, wrap each awaitable in `outside_uow(...)`: it clears the
  ContextVar for that task, so the call opens its own session as it would have
  before units of work existed. (On the in-memory test engine the pool holds a
  single connection, so escaping the unit while it holds that connection would
  deadlock — another reason gathers belong strictly outside `async with uow():`
  blocks.)

- **Do not suppress database errors inside a unit.** A failed statement aborts
  the whole transaction on PostgreSQL; catching the exception and issuing more
  statements on the same session cannot work. Domain "not found" outcomes that
  stores signal from `rowcount == 0` are fine — no statement failed — but an
  `IntegrityError` must propagate and roll the unit back.

- **Nesting is refused.** A unit inside a unit would silently merge two
  transactions that the code believes are independent; `UnitOfWork` raises
  instead.

Without an active unit nothing changes: `read_session` yields a fresh session
and never commits, `write_session` yields a fresh session and commits at exit —
exactly the per-call behavior every store had before.
"""

from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_T = TypeVar("_T")

_active_session: ContextVar[AsyncSession | None] = ContextVar("uow_session", default=None)

_BUSY_KEY = "uow_call_in_flight"
_CONCURRENT_USE = (
    "concurrent store calls inside a unit of work share one connection; "
    "run parallel reads outside the unit, each wrapped in outside_uow(...)"
)


class UnitOfWork:
    """One transaction over the store calls made inside its `async with` block.

    Constructed once at composition time with the same session factory the
    stores hold; the runner (or any other orchestrator) opens a unit per phase:

        async with self._uow():
            await self._messages.append(...)
            await self._tasks.mark_done(...)
            await self._exchanges.settle_owned(...)

    On a clean exit the unit commits; on an exception the whole phase rolls
    back together and nothing is persisted.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[None]:
        if _active_session.get() is not None:
            raise RuntimeError(
                "a unit of work is already active; nesting would merge two transactions"
            )
        async with self._session_factory() as session:
            token = _active_session.set(session)
            try:
                yield
                await session.commit()
            finally:
                _active_session.reset(token)


@asynccontextmanager
async def read_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """The active unit's session, or a fresh one that is never committed.

    An owned read-only session is simply closed at exit, rolling back its
    implicit transaction — the pre-unit-of-work behavior of every read.
    """
    async with _borrow(session_factory) as (session, _owned):
        yield session


@asynccontextmanager
async def write_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """The active unit's session (commit deferred to it), or a fresh one committed at exit."""
    async with _borrow(session_factory) as (session, owned):
        yield session
        if owned:
            await session.commit()


async def outside_uow(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run one store call outside the active unit of work, on its own session.

    The escape hatch for parallel reads while a unit is (or may be) active:
    each awaitable handed to `asyncio.gather` is wrapped, the wrapper clears
    the unit's ContextVar for that task alone, and the store call opens and
    commits its own session as it would with no unit around. `asyncio.gather`
    is used directly rather than through a wrapper here so its per-argument
    result typing survives.
    """
    token = _active_session.set(None)
    try:
        return await coro
    finally:
        _active_session.reset(token)


@asynccontextmanager
async def _borrow(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[tuple[AsyncSession, bool]]:
    """Yield `(session, owned)`: the unit's session unowned, or a fresh owned one.

    Marks the unit's session busy for the duration of the call — two store
    calls awaiting on one connection at once is always a bug, and this turns
    it from an obscure driver error into a `RuntimeError` that names the fix.
    """
    active = _active_session.get()
    if active is None:
        async with session_factory() as session:
            yield session, True
        return
    if active.info.get(_BUSY_KEY):
        raise RuntimeError(_CONCURRENT_USE)
    active.info[_BUSY_KEY] = True
    try:
        yield active, False
    finally:
        active.info[_BUSY_KEY] = False
