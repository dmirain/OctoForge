"""Alembic schema bootstrap: fresh migrate, legacy stamp, and drift guard."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, inspect, text

# Importing the models registers every table on Base.metadata for the drift check.
import octoforge_core.context.models
import octoforge_core.cron.models
import octoforge_core.datasets.models
import octoforge_core.db.models
import octoforge_core.instructions.models
import octoforge_core.memory.models  # noqa: F401
from octoforge_core.db.base import Base
from octoforge_core.db.engine import (
    _BASELINE_REVISION,
    _alembic_config,
    bootstrap_schema,
    create_engine,
    init_db,
)

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
        "dialog_summaries",
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


def _legacy_baseline_schema(connection: Connection) -> None:
    """Recreate a pre-Alembic database stuck at the baseline schema."""
    command.upgrade(_alembic_config(connection), _BASELINE_REVISION)
    connection.execute(text("DROP TABLE alembic_version"))


def _cron_job_columns(connection: Connection) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns("cron_jobs")}


def _alembic_head(connection: Connection) -> str:
    row = connection.execute(text("SELECT version_num FROM alembic_version")).one()
    return str(row[0])


async def test_bootstrap_upgrades_stale_legacy_database(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_legacy_baseline_schema)
        await bootstrap_schema(engine)  # stamp at baseline, then apply later migrations
        async with engine.connect() as connection:
            columns = await connection.run_sync(_cron_job_columns)
            head = await connection.run_sync(_alembic_head)
    finally:
        await engine.dispose()
    assert {"one_shot", "last_status", "last_error", "retry_count"} <= columns
    assert head != _BASELINE_REVISION


def _insert_legacy_tool_instruction(connection: Connection) -> None:
    """Insert a baseline-schema instruction row of the pre-rename type 'tool'."""
    connection.execute(
        text(
            "INSERT INTO instructions "
            "(id, type, title, content, embedding, tags, version, usage_count, success_count,"
            " created_at, updated_at) VALUES "
            "('legacy-1', 'tool', 'legacy_tool', '{}', '[]', '[]', 1, 0, 0,"
            " '2026-01-01 00:00:00+00:00', '2026-01-01 00:00:00+00:00')"
        )
    )


def _instruction_row(connection: Connection) -> tuple[str, int]:
    row = connection.execute(
        text("SELECT type, system FROM instructions WHERE id = 'legacy-1'")
    ).one()
    return str(row[0]), int(row[1])


def _instruction_columns(connection: Connection) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns("instructions")}


async def test_bootstrap_renames_tool_type_and_adds_system_flag(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_legacy_baseline_schema)
            await connection.run_sync(_insert_legacy_tool_instruction)
        await bootstrap_schema(engine)  # stamp at baseline, then apply later migrations
        async with engine.connect() as connection:
            row = await connection.run_sync(_instruction_row)
            columns = await connection.run_sync(_instruction_columns)
    finally:
        await engine.dispose()
    assert row == ("endpoint", 0)
    assert "system" in columns


def _table_columns(connection: Connection, table: str) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns(table)}


def _index_names(connection: Connection, table: str) -> set[str]:
    return {index["name"] for index in inspect(connection).get_indexes(table)}


async def test_bootstrap_adds_task_link_and_delivery_columns(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_legacy_baseline_schema)
        await bootstrap_schema(engine)  # stamp at baseline, then apply later migrations
        async with engine.connect() as connection:
            message_columns = await connection.run_sync(_table_columns, "messages")
            message_indexes = await connection.run_sync(_index_names, "messages")
            task_columns = await connection.run_sync(_table_columns, "tasks")
    finally:
        await engine.dispose()
    assert "task_id" in message_columns
    assert "ix_messages_task_id" in message_indexes
    assert "delivered_at" in task_columns


def _downgrade_one_step(connection: Connection) -> None:
    command.downgrade(_alembic_config(connection), "-1")


def _upgrade_to_head(connection: Connection) -> None:
    command.upgrade(_alembic_config(connection), "head")


async def test_task_link_and_delivery_migration_downgrades(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.run_sync(_downgrade_one_step)
        async with engine.connect() as connection:
            message_columns = await connection.run_sync(_table_columns, "messages")
            message_indexes = await connection.run_sync(_index_names, "messages")
            task_columns = await connection.run_sync(_table_columns, "tasks")
        assert "task_id" not in message_columns
        assert "ix_messages_task_id" not in message_indexes
        assert "delivered_at" not in task_columns
        # the downgrade is reversible: upgrading again restores the columns
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade_to_head)
        async with engine.connect() as connection:
            message_columns = await connection.run_sync(_table_columns, "messages")
            task_columns = await connection.run_sync(_table_columns, "tasks")
        assert "task_id" in message_columns
        assert "delivered_at" in task_columns
    finally:
        await engine.dispose()
