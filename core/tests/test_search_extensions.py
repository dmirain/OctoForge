"""The optional search extensions degrade quietly on a database that has none.

The Postgres side of this lives in `test_postgres_stores.py` (`make test-pg`),
because whether `CREATE EXTENSION vector` works is precisely the thing SQLite
cannot answer. What is worth pinning here is the other half of the contract:
every entry point stays callable, and answers "nothing", on a dialect where
none of this exists. That is the path a quickstart install and the whole test
suite take, and a store that raised instead of reporting absence would take
the process down at startup.
"""

from sqlalchemy.ext.asyncio import AsyncEngine

from octoforge_core.db.engine import create_engine
from octoforge_core.db.search_extensions import (
    OPTIONAL_EXTENSIONS,
    ensure_search_extensions,
    has_russian_unaccent,
    installed_search_extensions,
    missing,
)


async def _sqlite_engine() -> AsyncEngine:
    return create_engine("sqlite+aiosqlite:///:memory:")


async def test_creating_extensions_on_sqlite_is_a_no_op() -> None:
    engine = await _sqlite_engine()
    try:
        async with engine.begin() as connection:
            created = await connection.run_sync(ensure_search_extensions)
    finally:
        await engine.dispose()

    assert created == frozenset()


async def test_nothing_is_reported_installed_on_sqlite() -> None:
    engine = await _sqlite_engine()
    try:
        async with engine.connect() as connection:
            installed = await connection.run_sync(installed_search_extensions)
            russian = await connection.run_sync(has_russian_unaccent)
    finally:
        await engine.dispose()

    assert installed == frozenset()
    assert russian is False


def test_missing_lists_every_absent_extension_in_a_stable_order() -> None:
    assert missing(()) == OPTIONAL_EXTENSIONS
    assert missing(OPTIONAL_EXTENSIONS) == ()
    assert missing({"vector"}) == ("unaccent", "pg_textsearch")
