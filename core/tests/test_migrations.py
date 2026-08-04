"""Alembic schema bootstrap: fresh migrate, legacy stamp, and drift guard."""

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, inspect, text

# Importing the models registers every table on Base.metadata for the drift check.
from octoforge_core.db.base import Base
from octoforge_core.db.engine import (
    _BASELINE_REVISION,
    _alembic_config,
    bootstrap_schema,
    create_engine,
    init_db,
)
from octoforge_core.db.sqlite_fts import include_object
from octoforge_core.time import utc_now

EXPECTED_TABLES = frozenset(
    {
        "dialogs",
        "messages",
        "tasks",
        "cron_jobs",
        "instructions",
        "datasets",
        "dataset_records",
        "dialog_summaries",
    }
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'octoforge.db'}"


def _table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _metadata_diffs(connection: Connection) -> list[object]:
    # same filter env.py uses: the FTS5 mirrors are raw SQL and never appear in
    # Base.metadata, so autogenerate would propose dropping all of them
    context = MigrationContext.configure(connection, opts={"include_object": include_object})
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


async def test_task_exchange_link_is_backfilled_from_the_input(tmp_path: Path) -> None:
    """The column has to arrive carrying what the JSON already knew.

    Rows written before it existed keep their obligation inside `input`, and
    the code no longer looks there — an empty column would silently orphan
    every answer that was mid-flight when the migration ran.
    """
    engine = create_engine(_database_url(tmp_path))
    stamp = utc_now().isoformat()
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.run_sync(_downgrade_below_task_exchange)
            await connection.execute(
                sa.text(
                    "insert into dialogs (id, user_id, channel, created_at, updated_at) "
                    "values ('d-1', 'u-1', 'web', :now, :now)"
                ),
                {"now": stamp},
            )
            await connection.execute(
                sa.text(
                    "insert into exchanges (id, dialog_id, status, title, created_at, updated_at) "
                    "values ('ex-1', 'd-1', 'open', 'q', :now, :now)"
                ),
                {"now": stamp},
            )
            await connection.execute(
                sa.text(
                    "insert into tasks (id, dialog_id, user_id, channel, kind, title, "
                    "input, status, created_at) values "
                    "('t-answer', 'd-1', 'u-1', 'web', 'answer', 'q', :owed, 'running', :now), "
                    "('t-run', 'd-1', 'u-1', 'web', 'run', 'cron', :plain, 'running', :now)"
                ),
                {
                    "owed": json.dumps({"prompt": "q", "exchange_id": "ex-1"}),
                    "plain": json.dumps({"prompt": "sweep"}),
                    "now": stamp,
                },
            )
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade_to_head)
        async with engine.connect() as connection:
            rows = dict(
                (await connection.execute(sa.text("select id, exchange_id from tasks"))).all()
            )
    finally:
        await engine.dispose()
    assert rows["t-answer"] == "ex-1"
    assert rows["t-run"] is None  # a RUN task owes nobody, and must not be invented one


def _downgrade_below_task_exchange(connection: Connection) -> None:
    command.downgrade(_alembic_config(connection), "b6d4f2a91c85")


def _downgrade_below_task_link(connection: Connection) -> None:
    # explicit target: "-1" would undo whatever the newest migration is
    command.downgrade(_alembic_config(connection), "e5f8a2c1d394")


def _upgrade_to_head(connection: Connection) -> None:
    command.upgrade(_alembic_config(connection), "head")


async def test_task_link_and_delivery_migration_downgrades(tmp_path: Path) -> None:
    engine = create_engine(_database_url(tmp_path))
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.run_sync(_downgrade_below_task_link)
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


def _insert_legacy_memories(connection: Connection) -> None:
    """Rows as the pre-merge memories table held them (baseline schema)."""
    connection.execute(
        text(
            "INSERT INTO memories (id, user_id, key, content, tags, created_at, updated_at) "
            "VALUES "
            "('mem-1', 'user-a', 'city', 'Berlin', '[\"geo\"]',"
            " '2026-01-01 00:00:00', '2026-01-02 00:00:00'),"
            "('mem-2', NULL, 'shared', 'a global note', '[]',"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
    )


def _memory_instruction_rows(connection: Connection) -> list[tuple[str, str, str, str]]:
    rows = connection.execute(
        text(
            "SELECT id, title, COALESCE(owner_id, ''), embedding FROM instructions "
            "WHERE type = 'memory' ORDER BY id"
        )
    ).all()
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


async def test_memories_migrate_into_instructions(tmp_path: Path) -> None:
    """The data migration keeps every memory: owned rows stay owned, global go public.

    Ownership is asserted through the identity table rather than by the handle
    the row was written with: `b6d4f2a91c85` renumbers owners onto opaque ids,
    so the literal `user-a` is gone by the time the chain finishes — which is
    the point of it.
    """
    engine = create_engine(_database_url(tmp_path))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(_legacy_baseline_schema)
            await connection.run_sync(_insert_legacy_memories)
        await bootstrap_schema(engine)  # upgrades through f2a6c8d1e935
        async with engine.connect() as connection:
            rows = await connection.run_sync(_memory_instruction_rows)
            tables = await connection.run_sync(_table_names)
            owner = await connection.run_sync(_owner_of_identity, "web", "user-a")
    finally:
        await engine.dispose()
    assert rows == [
        ("mem-1", "city", owner, "[]"),  # empty embedding: the startup sweep fills it
        ("mem-2", "shared", "", "[]"),
    ]
    assert "memories" not in tables


def _owner_of_identity(connection: Connection, surface: str, external_id: str) -> str:
    """The opaque user id the old handle was renumbered onto."""
    row = connection.execute(
        text(
            "SELECT user_id FROM user_identities "
            "WHERE surface = :surface AND external_id = :external_id"
        ),
        {"surface": surface, "external_id": external_id},
    ).first()
    assert row is not None, f"no identity for {surface}:{external_id}"
    return str(row[0])


def _downgrade_below_secrets_description(connection: Connection) -> None:
    command.downgrade(_alembic_config(connection), "f1b9d4e8c257")


async def test_legacy_secrets_get_a_description_backfilled(tmp_path: Path) -> None:
    """Rows from before the requirement must not stay NULL: the column tightens
    to NOT NULL, and the placeholder itself tells the agent to ask the user."""
    engine = create_engine(_database_url(tmp_path))
    stamp = utc_now().isoformat()
    try:
        await bootstrap_schema(engine)
        async with engine.begin() as connection:
            await connection.run_sync(_downgrade_below_secrets_description)
            await connection.execute(
                sa.text(
                    "insert into secrets (id, user_id, code, ciphertext, allowed_host, "
                    "created_at) values ('s-1', 'u-1', 'old_token', 'cipher', "
                    "'api.example.com', :now)"
                ),
                {"now": stamp},
            )
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade_to_head)
        async with engine.connect() as connection:
            (description,) = (
                await connection.execute(sa.text("select description from secrets"))
            ).one()
            nullable = (
                await connection.run_sync(
                    lambda sync: {
                        c["name"]: c["nullable"] for c in sa.inspect(sync).get_columns("secrets")
                    }
                )
            )["description"]
    finally:
        await engine.dispose()
    assert description is not None and "secret_link" in description
    assert nullable is False
