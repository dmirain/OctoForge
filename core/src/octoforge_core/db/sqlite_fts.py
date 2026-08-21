"""Public FTS5 helpers for embedded SQLite deployments and migrations."""

from octoforge_core.db.sqlite_fts_schema import (
    ensure_sqlite_fts,
    has_sqlite_fts,
    include_object,
)
from octoforge_core.db.sqlite_search import FTS_TABLES, FtsTable, match_expression, rank_expression

__all__ = [
    "FTS_TABLES",
    "FtsTable",
    "ensure_sqlite_fts",
    "has_sqlite_fts",
    "include_object",
    "match_expression",
    "rank_expression",
]
