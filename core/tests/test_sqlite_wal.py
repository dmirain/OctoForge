"""A file-backed SQLite database runs in WAL mode.

Not a style preference. The default rollback journal takes an exclusive lock
for the whole of every write, so a reader arriving mid-write gets `database is
locked` immediately instead of waiting — and one process here serves every
dialog, with the actor, the cron scheduler and the recovery sweeps all touching
the database at once.
"""

from pathlib import Path

from sqlalchemy import text

from octoforge_core.db.engine import SQLITE_BUSY_TIMEOUT_MS, create_engine


async def test_a_file_database_is_opened_in_wal_mode(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'of.db'}")
    try:
        async with engine.connect() as connection:
            journal = await connection.scalar(text("PRAGMA journal_mode"))
            busy = await connection.scalar(text("PRAGMA busy_timeout"))
            synchronous = await connection.scalar(text("PRAGMA synchronous"))
    finally:
        await engine.dispose()

    assert str(journal).lower() == "wal"
    assert busy == SQLITE_BUSY_TIMEOUT_MS
    assert synchronous == 1  # NORMAL: crash-safe under WAL, no fsync per commit


async def test_an_in_memory_database_keeps_its_shared_cache_setup() -> None:
    """WAL does not apply to the shared-cache in-memory branch, and forcing it
    there would undo the pooling the tests depend on."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.connect() as connection:
            journal = await connection.scalar(text("PRAGMA journal_mode"))
    finally:
        await engine.dispose()

    assert str(journal).lower() != "wal"
