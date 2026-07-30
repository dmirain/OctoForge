"""BM25 over message history and dataset descriptions

Revision ID: d5a3f8c1e627
Revises: c7e2a91f4d38
Create Date: 2026-07-30

`history_search` was a substring match: `content ILIKE '%query%'`, ordered by
seq, over every message in the dialog. Two consequences, both bad in Russian.
"задача" did not find "задачи" — no stemming, so the tool worked properly only
when the user happened to type the exact inflected form that was written months
ago. And the scan was linear in the dialog's whole history, on a table that
never deletes rows.

BM25 over `messages.content` fixes both: the index stems through the same
`russian_unaccent` configuration the rest of retrieval uses, and ranks by
relevance instead of returning the first `limit` substring hits in seq order.

`datasets.description` covers the other half of naming things: finding a
dataset by a word from its description rather than by cosine similarity to it.

Skipped entirely without pg_textsearch, which is the normal case on managed
Postgres and on SQLite — there `history_search` keeps its ILIKE behaviour.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5a3f8c1e627"
down_revision: str | None = "c7e2a91f4d38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

POSTGRESQL = "postgresql"
TEXT_CONFIG = "public.russian_unaccent"
FALLBACK_TEXT_CONFIG = "pg_catalog.russian"
INDEXES = (
    ("ix_messages_bm25_content", "messages", "content"),
    ("ix_datasets_bm25_description", "datasets", "description"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != POSTGRESQL:
        return
    installed = bind.scalar(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'pg_textsearch'")
    )
    if installed != 1:
        logger.warning(
            "pg_textsearch is not installed; history_search stays a substring match"
        )
        return
    config = TEXT_CONFIG if _has_text_config(bind) else FALLBACK_TEXT_CONFIG
    for name, table, column in INDEXES:
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table} "
                f"USING bm25 ({column}) WITH (text_config='{config}')"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != POSTGRESQL:
        return
    for name, _table, _column in INDEXES:
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))


def _has_text_config(bind: sa.Connection) -> bool:
    """Whether the accent-folding configuration from a2f7c5e9d148 exists."""
    found = bind.scalar(
        sa.text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'russian_unaccent'")
    )
    return found == 1
