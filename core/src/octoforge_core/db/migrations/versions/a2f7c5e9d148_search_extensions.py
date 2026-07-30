"""search extensions and the accent-folding russian text config

Revision ID: a2f7c5e9d148
Revises: f8b1d3c7a642
Create Date: 2026-07-30

Prepares the database for hybrid retrieval. Creates nothing that anything
queries yet — the columns and indexes arrive in later revisions — so this can
ship on its own and be verified before behavior changes.

Three extensions, all OPTIONAL:

- `vector` (pgvector) backs nearest-neighbour search over embeddings.
- `pg_textsearch` adds a real BM25 index type. It needs
  `shared_preload_libraries`, so managed Postgres (RDS, Cloud SQL, Supabase,
  Neon) cannot have it at all.
- `unaccent` exists to build `public.russian_unaccent`: the stock `russian`
  configuration treats ё as a letter of its own, so a query spelled without
  the diaeresis would not find a record that has it.

Everything here is therefore best-effort. Each statement runs in its own
SAVEPOINT: an installation whose application role is not a superuser, or whose
Postgres ships without one of these, gets a database that is missing the
capability rather than a migration that refuses to complete. The application
probes for what actually exists at startup and degrades to in-process
brute-force ranking, so a missing extension costs recall quality, not uptime.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2f7c5e9d148"
down_revision: str | None = "f8b1d3c7a642"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

POSTGRESQL = "postgresql"
RUSSIAN_UNACCENT = "russian_unaccent"
# Cyrillic words tokenize as `word`; the hyphenated variants are the same
# tokens inside compound words. The ascii* types are left alone deliberately —
# unaccent is a no-op on ASCII, and touching them only widens the diff.
STEMMED_TOKEN_TYPES = "hword, hword_part, word"
OPTIONAL_EXTENSIONS = ("vector", "unaccent", "pg_textsearch")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != POSTGRESQL:
        return  # SQLite ranks in process and uses FTS5 for its lexical half
    available = {name for name in OPTIONAL_EXTENSIONS if _try_create_extension(name)}
    if "unaccent" in available:
        _create_russian_unaccent()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != POSTGRESQL:
        return
    # The extensions are left in place: dropping one would take any index built
    # on it with it, and they are harmless when unused.
    op.execute(sa.text(f"DROP TEXT SEARCH CONFIGURATION IF EXISTS public.{RUSSIAN_UNACCENT}"))


def _try_create_extension(name: str) -> bool:
    """Create the extension, reporting rather than failing when it cannot be.

    Wrapped in a SAVEPOINT because a failed statement otherwise poisons the
    whole migration transaction: without it, one missing extension would roll
    back the text search configuration built after it.
    """
    bind = op.get_bind()
    savepoint = bind.begin_nested()
    try:
        bind.execute(sa.text(f"CREATE EXTENSION IF NOT EXISTS {name}"))
    except sa.exc.DatabaseError as error:
        savepoint.rollback()
        logger.warning(
            "optional extension %s is unavailable, search degrades without it: %s",
            name,
            _first_line(error),
        )
        return False
    savepoint.commit()
    return True


def _create_russian_unaccent() -> None:
    """Add a russian configuration that strips the diaeresis before stemming.

    `CREATE TEXT SEARCH CONFIGURATION` has no IF NOT EXISTS, so existence is
    checked first — this migration must stay re-runnable against a database
    that was prepared by hand.
    """
    bind = op.get_bind()
    exists = bind.scalar(
        sa.text("SELECT 1 FROM pg_ts_config WHERE cfgname = :name"),
        {"name": RUSSIAN_UNACCENT},
    )
    if exists:
        return
    bind.execute(
        sa.text(
            f"CREATE TEXT SEARCH CONFIGURATION public.{RUSSIAN_UNACCENT} "
            "(COPY = pg_catalog.russian)"
        )
    )
    bind.execute(
        sa.text(
            f"ALTER TEXT SEARCH CONFIGURATION public.{RUSSIAN_UNACCENT} "
            f"ALTER MAPPING FOR {STEMMED_TOKEN_TYPES} WITH unaccent, russian_stem"
        )
    )


def _first_line(error: Exception) -> str:
    """Collapse a driver error to its first line — the rest is a SQL echo."""
    return str(error).strip().splitlines()[0]
