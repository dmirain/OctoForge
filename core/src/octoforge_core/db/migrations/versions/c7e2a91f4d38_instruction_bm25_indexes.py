"""BM25 indexes over instruction titles and bodies

Revision ID: c7e2a91f4d38
Revises: b4d9e1a7c236
Create Date: 2026-07-30

The lexical half of retrieval. Embeddings answer "what is this about", which is
the wrong question for a product name, an error code, an API field or a rare
acronym — there only one string will do, and a nearest-neighbour search happily
returns four documents on the same topic that never mention it.

Two indexes rather than one over `title || content`, because BM25 normalizes
relevance by document length: a title runs a couple of tokens and a skill body
well over a hundred, so a single index would let the body drown a title match.
Kept apart they are two independent signals, and the fusion upstream weighs
them separately.

`public.russian_unaccent` is schema-qualified deliberately. Without the schema
pg_textsearch fails the index build with "text search configuration does not
exist" even when the configuration is right there in the search path — found
the hard way, so the qualification is not cosmetic.

Skipped entirely without pg_textsearch, which is the normal case on managed
Postgres (it needs `shared_preload_libraries`) and on SQLite. Recall then runs
on embeddings alone, which is what it did before this revision.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e2a91f4d38"
down_revision: str | None = "b4d9e1a7c236"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

POSTGRESQL = "postgresql"
TABLE = "instructions"
TEXT_CONFIG = "public.russian_unaccent"
FALLBACK_TEXT_CONFIG = "pg_catalog.russian"
INDEXES = (
    ("ix_instructions_bm25_content", "content"),
    ("ix_instructions_bm25_title", "title"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != POSTGRESQL:
        return
    if not _has_extension(bind, "pg_textsearch"):
        logger.warning(
            "pg_textsearch is not installed; recall stays embeddings-only "
            "(this is expected on managed Postgres, which cannot preload it)"
        )
        return
    config = TEXT_CONFIG if _has_text_config(bind) else FALLBACK_TEXT_CONFIG
    for name, column in INDEXES:
        bind.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS {name} ON {TABLE} "
                f"USING bm25 ({column}) WITH (text_config='{config}')"
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != POSTGRESQL:
        return
    for name, _column in INDEXES:
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))


def _has_extension(bind: sa.Connection, name: str) -> bool:
    return bind.scalar(sa.text("SELECT 1 FROM pg_extension WHERE extname = :n"), {"n": name}) == 1


def _has_text_config(bind: sa.Connection) -> bool:
    """Whether the accent-folding configuration from a2f7c5e9d148 was created.

    It needs `unaccent`; an installation that could not create that extension
    falls back to the stock russian configuration, which still stems but keeps
    the diaeresis distinct.
    """
    found = bind.scalar(
        sa.text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'russian_unaccent'")
    )
    return found == 1
