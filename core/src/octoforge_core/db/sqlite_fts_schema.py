"""Create, probe and hide raw-SQL FTS5 mirrors."""

import logging

import sqlalchemy as sa
from sqlalchemy import Connection

from octoforge_core.db.sqlite_search import FTS_SUFFIX, FTS_TABLES, TOKENIZER, FtsTable

logger = logging.getLogger(__name__)

SQLITE = "sqlite"
ALEMBIC_CALLBACK_ARITY = 5


def ensure_sqlite_fts(connection: Connection) -> bool:
    """Create every mirror and trigger, degrading cleanly without FTS5."""
    if connection.dialect.name != SQLITE:
        return False
    for table in FTS_TABLES:
        try:
            _create_mirror(connection, table)
        except sa.exc.DatabaseError as error:
            logger.warning(
                "could not create the %s FTS5 mirror, lexical search stays off: %s",
                table.name,
                str(error).strip().splitlines()[0],
            )
            return False
    return True


def has_sqlite_fts(connection: Connection) -> bool:
    """Whether every FTS5 mirror exists."""
    if connection.dialect.name != SQLITE:
        return False
    present = {
        str(name)
        for (name,) in connection.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
    }
    return all(table.name in present for table in FTS_TABLES)


def _create_mirror(connection: Connection, table: FtsTable) -> None:
    columns = ", ".join(table.columns)
    new_values = ", ".join(f"new.{column}" for column in table.columns)
    old_values = ", ".join(f"old.{column}" for column in table.columns)
    connection.execute(
        sa.text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table.name} USING fts5("
            f"{columns}, content='{table.source}', content_rowid='rowid', "
            f"tokenize='{TOKENIZER}')"
        )
    )
    connection.execute(
        sa.text(
            f"CREATE TRIGGER IF NOT EXISTS {table.name}_ai AFTER INSERT ON {table.source} "
            f"BEGIN INSERT INTO {table.name}(rowid, {columns}) "
            f"VALUES (new.rowid, {new_values}); END"
        )
    )
    connection.execute(
        sa.text(
            f"CREATE TRIGGER IF NOT EXISTS {table.name}_ad AFTER DELETE ON {table.source} "
            f"BEGIN INSERT INTO {table.name}({table.name}, rowid, {columns}) "
            f"VALUES ('delete', old.rowid, {old_values}); END"
        )
    )
    connection.execute(
        sa.text(
            f"CREATE TRIGGER IF NOT EXISTS {table.name}_au AFTER UPDATE ON {table.source} "
            f"BEGIN INSERT INTO {table.name}({table.name}, rowid, {columns}) "
            f"VALUES ('delete', old.rowid, {old_values}); "
            f"INSERT INTO {table.name}(rowid, {columns}) "
            f"VALUES (new.rowid, {new_values}); END"
        )
    )
    connection.execute(sa.text(f"INSERT INTO {table.name}({table.name}) VALUES ('rebuild')"))


def include_object(*values: object) -> bool:
    """Alembic callback that hides FTS5 mirrors and their shadow tables."""
    if len(values) != ALEMBIC_CALLBACK_ARITY:
        raise TypeError("include_object expects five Alembic callback values")
    name = values[1]
    type_ = values[2]
    return not (type_ == "table" and isinstance(name, str) and FTS_SUFFIX in name)
