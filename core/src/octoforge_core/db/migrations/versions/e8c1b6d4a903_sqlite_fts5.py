"""FTS5 lexical search on SQLite

Revision ID: e8c1b6d4a903
Revises: d5a3f8c1e627
Create Date: 2026-07-30

The embedded counterpart of the pg_textsearch indexes: an installation running
`core/` on a database file gets the lexical half of retrieval too. Postgres
deployments are unaffected — this revision does nothing there.

External-content FTS5 mirrors of `instructions`, `messages` and `datasets`,
kept in sync by triggers, tokenized with `trigram`. The tokenizer choice is the
whole story and is documented in `db/sqlite_fts.py`: SQLite has no Russian
stemmer, and trigram's substring matching is the closest available substitute —
it finds "задачи" from "задач" but not from "задача".

Deliberately a migration and not part of `create_all`. The test suite builds
its schema with `init_db`, and creating these there would move ~40 existing
tests off the brute-force ranking path onto the fusion path without anyone
noticing, leaving the no-lexical-search behaviour untested.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8c1b6d4a903"
down_revision: str | None = "d5a3f8c1e627"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

SQLITE = "sqlite"
TOKENIZER = "trigram remove_diacritics 1"
MIRRORS = (
    ("instructions_fts", "instructions", ("title", "content")),
    ("messages_fts", "messages", ("content",)),
    ("datasets_fts", "datasets", ("description",)),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != SQLITE:
        return
    for name, source, columns in MIRRORS:
        try:
            _create_mirror(bind, name, source, columns)
        except sa.exc.DatabaseError as error:
            # A SQLite build without FTS5 is unusual but real; it must degrade
            # to embeddings-only rather than stop the upgrade.
            logger.warning(
                "could not create %s, lexical search stays off on this build: %s",
                name,
                str(error).strip().splitlines()[0],
            )
            return


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != SQLITE:
        return
    for name, _source, _columns in MIRRORS:
        for suffix in ("ai", "ad", "au"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}_{suffix}"))
        op.execute(sa.text(f"DROP TABLE IF EXISTS {name}"))


def _create_mirror(
    bind: sa.Connection,
    name: str,
    source: str,
    columns: tuple[str, ...],
) -> None:
    joined = ", ".join(columns)
    new_values = ", ".join(f"new.{column}" for column in columns)
    old_values = ", ".join(f"old.{column}" for column in columns)
    bind.execute(
        sa.text(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {name} USING fts5("
            f"{joined}, content='{source}', content_rowid='rowid', "
            f"tokenize='{TOKENIZER}')"
        )
    )
    bind.execute(
        sa.text(
            f"CREATE TRIGGER IF NOT EXISTS {name}_ai AFTER INSERT ON {source} "
            f"BEGIN INSERT INTO {name}(rowid, {joined}) "
            f"VALUES (new.rowid, {new_values}); END"
        )
    )
    bind.execute(
        sa.text(
            f"CREATE TRIGGER IF NOT EXISTS {name}_ad AFTER DELETE ON {source} "
            f"BEGIN INSERT INTO {name}({name}, rowid, {joined}) "
            f"VALUES ('delete', old.rowid, {old_values}); END"
        )
    )
    bind.execute(
        sa.text(
            f"CREATE TRIGGER IF NOT EXISTS {name}_au AFTER UPDATE ON {source} "
            f"BEGIN INSERT INTO {name}({name}, rowid, {joined}) "
            f"VALUES ('delete', old.rowid, {old_values}); "
            f"INSERT INTO {name}(rowid, {joined}) VALUES (new.rowid, {new_values}); END"
        )
    )
    # rows that predate the triggers
    bind.execute(sa.text(f"INSERT INTO {name}({name}) VALUES ('rebuild')"))
