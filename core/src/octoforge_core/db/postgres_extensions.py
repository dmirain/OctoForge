"""Best-effort setup and probing of optional Postgres search extensions."""

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
STEMMED_TOKEN_TYPES = "hword, hword_part, word"


def ensure_search_extensions(connection: Connection) -> frozenset[str]:
    """Create what the current database role allows and report the result."""
    if connection.dialect.name != POSTGRESQL:
        return frozenset()
    available = frozenset(
        name for name in OPTIONAL_EXTENSIONS if _create_extension(connection, name)
    )
    if UNACCENT in available:
        _create_russian_unaccent(connection)
    return available


def installed_search_extensions(connection: Connection) -> frozenset[str]:
    """Probe installed optional extensions without changing the database."""
    if connection.dialect.name != POSTGRESQL:
        return frozenset()
    rows = connection.execute(
        sa.text("SELECT extname FROM pg_extension WHERE extname = ANY(:names)"),
        {"names": list(OPTIONAL_EXTENSIONS)},
    )
    return frozenset(str(name) for (name,) in rows)


def has_russian_unaccent(connection: Connection) -> bool:
    """Whether the accent-folding Russian text configuration exists."""
    if connection.dialect.name != POSTGRESQL:
        return False
    found = connection.scalar(
        sa.text("SELECT 1 FROM pg_ts_config WHERE cfgname = :name"),
        {"name": RUSSIAN_UNACCENT},
    )
    return found is not None


def missing(available: Iterable[str]) -> tuple[str, ...]:
    """Return absent optional extensions in stable order."""
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
            "could not create %s, lexical search will not fold the diaeresis: %s",
            RUSSIAN_UNACCENT,
            str(error).strip().splitlines()[0],
        )
        return
    savepoint.commit()
