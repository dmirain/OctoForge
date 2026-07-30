"""FTS5 lexical search for the embedded (SQLite) deployment.

The Postgres path gets BM25 from pg_textsearch. SQLite has BM25 built in, so an
installation embedding `core/` with a database file can have the lexical half
too — with one honest caveat, stated here because it changes what users
experience:

**There is no Russian stemmer.** Postgres stems through `russian_unaccent`, so
"задача" finds "задачи". SQLite has no such tokenizer, and the closest thing
available is `trigram`, which matches substrings. That covers the common
direction — a stem is a shared prefix, so "задач" finds "задачи" and "договор"
finds "договора" — but not the other one: "задача" is not a substring of
"задачи" and will not match it. Latin technical terms (error codes, product
names, API fields), which is the highest-value case for lexical search, work
identically on both dialects.

Trigram is chosen over `unicode61` deliberately. `unicode61` matches only the
exact word form, which for an inflected language means the user has to guess
the exact form somebody typed months ago — the very failure that made
`history_search` weak before any of this.

Created by migration only, never by `create_all`. That is not an oversight: the
test suite builds its schema with `init_db`, and creating these tables there
would silently move ~40 existing tests off the brute-force ranking path onto
the fusion path, leaving the no-extension behaviour untested.
"""

import logging
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection

logger = logging.getLogger(__name__)

SQLITE = "sqlite"
# Substring matching needs at least a trigram; shorter tokens match nothing and
# are dropped rather than sent, so a query of only short words returns no hits
# instead of every row.
MIN_TOKEN_LENGTH = 3
# Column weights for `bm25()`. A title is a couple of tokens and a body over a
# hundred, and BM25 normalizes by length, so without this a title match would
# barely register. Postgres achieves the same with two separate indexes; here
# one virtual table with weights is simpler and the difference is not
# observable through the port.
TITLE_WEIGHT = 10.0
BODY_WEIGHT = 1.0
TOKENIZER = "trigram remove_diacritics 1"
# Every FTS5 object this module creates carries it, shadow tables included.
FTS_SUFFIX = "_fts"


@dataclass(frozen=True, slots=True)
class FtsTable:
    """One FTS5 mirror: which table it shadows and which columns it indexes."""

    name: str
    source: str
    columns: tuple[str, ...]


FTS_TABLES = (
    FtsTable("instructions_fts", "instructions", ("title", "content")),
    FtsTable("messages_fts", "messages", ("content",)),
    FtsTable("datasets_fts", "datasets", ("description",)),
)


def ensure_sqlite_fts(connection: Connection) -> bool:
    """Create the FTS5 mirrors and their sync triggers; report whether they exist.

    Idempotent. Returns False on any other dialect, and on a SQLite build
    without FTS5 — an unusual but real configuration, and one that must degrade
    to embeddings-only rather than fail to start.
    """
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
    """Whether every FTS5 mirror exists (the composition root's probe)."""
    if connection.dialect.name != SQLITE:
        return False
    present = {
        str(name)
        for (name,) in connection.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
    }
    return all(table.name in present for table in FTS_TABLES)


def match_expression(query: str) -> str | None:
    """Turn a user query into a safe FTS5 MATCH expression, or None if nothing is left.

    Every token is emitted as a quoted phrase. This is not cosmetic: FTS5 MATCH
    is a query language, so an unescaped `AND` or a trailing `*` is a syntax
    error raised at the user, and a stray `"` changes what is searched. Tokens
    are joined with OR because BM25 ranks by how much of the query a document
    covers — the same any-term semantics the Postgres side has.
    """
    tokens = [token for token in query.split() if len(token.strip('"')) >= MIN_TOKEN_LENGTH]
    if not tokens:
        return None
    quoted = ['"' + token.replace('"', '""') + '"' for token in tokens]
    return " OR ".join(quoted)


def rank_expression(table: FtsTable) -> str:
    """The `bm25(...)` call for this mirror, with title weighted where there is one."""
    if len(table.columns) == 1:
        return f"bm25({table.name})"
    weights = ", ".join(
        str(TITLE_WEIGHT if column == "title" else BODY_WEIGHT) for column in table.columns
    )
    return f"bm25({table.name}, {weights})"


def _create_mirror(connection: Connection, table: FtsTable) -> None:
    """Create one external-content FTS5 table, its triggers and its backfill."""
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
    # External content keeps no copy of the text: the mirror stores only the
    # index and reads the row back through content_rowid, so these triggers are
    # what keeps it true rather than an optimisation.
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
    # Rows that already exist predate the triggers; 'rebuild' indexes them from
    # the source table, and is a no-op on an empty one.
    connection.execute(sa.text(f"INSERT INTO {table.name}({table.name}) VALUES ('rebuild')"))


def include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """Alembic autogenerate filter: ignore the FTS5 mirrors and their shadows.

    The mirrors are created in raw SQL by migration `e8c1b6d4a903` and cannot
    be part of `Base.metadata` — SQLAlchemy has no virtual-table construct —
    and SQLite builds four shadow tables beside each one (`_data`, `_idx`,
    `_docsize`, `_config`). Without this filter autogenerate sees tables the
    models do not declare and proposes dropping every one of them.
    """
    return not (type_ == "table" and name is not None and FTS_SUFFIX in name)
