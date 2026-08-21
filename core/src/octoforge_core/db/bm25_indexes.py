"""Fresh-database setup of pg_textsearch BM25 indexes."""

import logging

import sqlalchemy as sa
from sqlalchemy import Connection

from octoforge_core.db.postgres_extensions import (
    PG_TEXTSEARCH,
    POSTGRESQL,
    RUSSIAN_UNACCENT,
    has_russian_unaccent,
    installed_search_extensions,
)

logger = logging.getLogger(__name__)

BM25_INDEXES = (
    ("ix_instructions_bm25_content", "instructions", "content"),
    ("ix_instructions_bm25_title", "instructions", "title"),
    ("ix_messages_bm25_content", "messages", "content"),
    ("ix_datasets_bm25_description", "datasets", "description"),
)
FALLBACK_TEXT_CONFIG = "pg_catalog.russian"


def ensure_bm25_indexes(connection: Connection) -> bool:
    """Build expected lexical indexes when pg_textsearch is available."""
    if connection.dialect.name != POSTGRESQL:
        return False
    if PG_TEXTSEARCH not in installed_search_extensions(connection):
        return False
    config = (
        f"public.{RUSSIAN_UNACCENT}" if has_russian_unaccent(connection) else FALLBACK_TEXT_CONFIG
    )
    savepoint = connection.begin_nested()
    try:
        for name, table, column in BM25_INDEXES:
            connection.execute(
                sa.text(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
                    f"USING bm25 ({column}) WITH (text_config='{config}')"
                )
            )
    except sa.exc.DatabaseError as error:
        savepoint.rollback()
        logger.warning(
            "could not build the BM25 indexes, recall stays embeddings-only: %s",
            str(error).strip().splitlines()[0],
        )
        return False
    savepoint.commit()
    return True
