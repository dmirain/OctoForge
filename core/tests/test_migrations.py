"""Alembic schema bootstrap: fresh migrate, legacy stamp, and drift guard."""

from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, inspect

# Importing the models registers every table on Base.metadata for the drift check.
import octoforge_core.cron.models
import octoforge_core.datasets.models
import octoforge_core.db.models
import octoforge_core.instructions.models
import octoforge_core.memory.models  # noqa: F401
from octoforge_core.db.base import Base
from octoforge_core.db.engine import bootstrap_schema, create_engine, init_db

EXPECTED_TABLES = frozenset(
    {
        "dialogs",
        "messages",
        "tasks",
        "cron_jobs",
        "instructions",
        "datasets",
        "dataset_records",
        "memories",
    }
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'octoforge.db'}"


def _table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _metadata_diffs(connection: Connection) -> list[object]:
    context = MigrationContext.configure(connection)
    return list(compare_metadata(context, Base.metadata))


async def test_bootstrap_creates_all_tables(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            tables = await connection.run_sync(_table_names)
    finally:
        await engine.dispose()
    assert "alembic_version" in tables  # database is now Alembic-managed
    assert tables >= EXPECTED_TABLES


async def test_baseline_has_no_autogenerate_drift(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        await bootstrap_schema(engine)
        async with engine.connect() as connection:
            diffs = await connection.run_sync(_metadata_diffs)
    finally:
        await engine.dispose()
    assert diffs == []  # the baseline migration matches the ORM metadata


async def test_bootstrap_stamps_pre_alembic_database(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        await init_db(engine)  # legacy database: tables exist, no alembic_version
        await bootstrap_schema(engine)  # must stamp (not re-create) without error
        async with engine.connect() as connection:
            tables = await connection.run_sync(_table_names)
    finally:
        await engine.dispose()
    assert "alembic_version" in tables
    assert tables >= EXPECTED_TABLES
