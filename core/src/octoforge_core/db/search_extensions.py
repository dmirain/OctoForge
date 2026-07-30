"""Optional Postgres search extensions: create them if possible, report what is there.

Hybrid retrieval wants three things from Postgres, and none of them is
guaranteed to exist:

- `vector` (pgvector) for nearest-neighbour search over embeddings;
- `pg_textsearch` for BM25-ranked lexical search — it needs
  `shared_preload_libraries`, which managed Postgres (RDS, Cloud SQL, Supabase,
  Neon) does not let you set, so those installations simply cannot have it;
- `unaccent`, used to build the `russian_unaccent` text search configuration,
  because the stock `russian` config treats ё as a letter of its own, so a
  query spelled without the diaeresis would not find a record that has it.

Absence is a supported state, not an error. Everything here is best-effort and
returns what it managed to get; the composition root reports the result and
picks a store implementation to match, and search falls back to in-process
brute-force cosine when the database cannot help.

Deliberate duplication: migration `a2f7c5e9d148` contains its own copy of this
recipe. Migrations are frozen in time — one must keep doing what it did when it
was written — while this module tracks what a *new* database should get, which
is the same split the codebase already makes between `create_all` and the
migration chain (see `db/engine.py:_create_and_stamp`).
"""

import logging
from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy import Connection

logger = logging.getLogger(__name__)

POSTGRESQL = "postgresql"

VECTOR = "vector"
UNACCENT = "unaccent"
PG_TEXTSEARCH = "pg_textsearch"
OPTIONAL_EXTENSIONS = (VECTOR, UNACCENT, PG_TEXTSEARCH)

RUSSIAN_UNACCENT = "russian_unaccent"
# Cyrillic words tokenize as `word`; the hyphenated variants are the same tokens
# inside compound words. The ascii* token types keep the plain stemmer —
# unaccent is a no-op on ASCII.
STEMMED_TOKEN_TYPES = "hword, hword_part, word"


def ensure_search_extensions(connection: Connection) -> frozenset[str]:
    """Create the optional extensions and text config; return what now exists.

    Safe to call repeatedly and safe to call against a database whose role
    cannot create extensions: each statement runs in its own SAVEPOINT, so one
    refusal neither aborts the surrounding transaction nor stops the others
    from being tried.
    """
    if connection.dialect.name != POSTGRESQL:
        return frozenset()
    available = frozenset(
        name for name in OPTIONAL_EXTENSIONS if _create_extension(connection, name)
    )
    if UNACCENT in available:
        _create_russian_unaccent(connection)
    return available


def installed_search_extensions(connection: Connection) -> frozenset[str]:
    """Return which of the optional extensions this database actually has.

    Read-only: the startup capability report and the store selection both need
    to know the truth about a database nobody is allowed to modify.
    """
    if connection.dialect.name != POSTGRESQL:
        return frozenset()
    rows = connection.execute(
        sa.text("SELECT extname FROM pg_extension WHERE extname = ANY(:names)"),
        {"names": list(OPTIONAL_EXTENSIONS)},
    )
    return frozenset(str(name) for (name,) in rows)


def has_russian_unaccent(connection: Connection) -> bool:
    """Whether the accent-folding russian text search configuration is present."""
    if connection.dialect.name != POSTGRESQL:
        return False
    found = connection.scalar(
        sa.text("SELECT 1 FROM pg_ts_config WHERE cfgname = :name"),
        {"name": RUSSIAN_UNACCENT},
    )
    return found is not None


def missing(available: Iterable[str]) -> tuple[str, ...]:
    """Return the optional extensions that are not in `available`, in a stable order."""
    present = set(available)
    return tuple(name for name in OPTIONAL_EXTENSIONS if name not in present)


def _create_extension(connection: Connection, name: str) -> bool:
    savepoint = connection.begin_nested()
    try:
        connection.execute(sa.text(f"CREATE EXTENSION IF NOT EXISTS {name}"))
    except sa.exc.DatabaseError as error:
        savepoint.rollback()
        logger.warning(
            "optional extension %s is unavailable, search degrades without it: %s",
            name,
            str(error).strip().splitlines()[0],
        )
        return False
    savepoint.commit()
    return True


def _create_russian_unaccent(connection: Connection) -> None:
    """Add a russian configuration that strips the diaeresis before stemming."""
    if has_russian_unaccent(connection):
        return
    savepoint = connection.begin_nested()
    try:
        connection.execute(
            sa.text(
                f"CREATE TEXT SEARCH CONFIGURATION public.{RUSSIAN_UNACCENT} "
                "(COPY = pg_catalog.russian)"
            )
        )
        connection.execute(
            sa.text(
                f"ALTER TEXT SEARCH CONFIGURATION public.{RUSSIAN_UNACCENT} "
                f"ALTER MAPPING FOR {STEMMED_TOKEN_TYPES} WITH unaccent, russian_stem"
            )
        )
    except sa.exc.DatabaseError as error:
        savepoint.rollback()
        logger.warning(
            "could not create the %s text search configuration, "
            "lexical search will not fold the diaeresis in ё: %s",
            RUSSIAN_UNACCENT,
            str(error).strip().splitlines()[0],
        )
        return
    savepoint.commit()
