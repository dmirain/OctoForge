"""Public optional Postgres search-extension setup and probes."""

from octoforge_core.db.bm25_indexes import BM25_INDEXES, ensure_bm25_indexes
from octoforge_core.db.postgres_extensions import (
    OPTIONAL_EXTENSIONS,
    PG_TEXTSEARCH,
    RUSSIAN_UNACCENT,
    UNACCENT,
    VECTOR,
    ensure_search_extensions,
    has_russian_unaccent,
    installed_search_extensions,
    missing,
)

__all__ = [
    "BM25_INDEXES",
    "OPTIONAL_EXTENSIONS",
    "PG_TEXTSEARCH",
    "RUSSIAN_UNACCENT",
    "UNACCENT",
    "VECTOR",
    "ensure_bm25_indexes",
    "ensure_search_extensions",
    "has_russian_unaccent",
    "installed_search_extensions",
    "missing",
]
