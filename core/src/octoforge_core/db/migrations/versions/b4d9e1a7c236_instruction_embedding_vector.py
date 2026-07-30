"""a searchable vector column and the model that produced it

Revision ID: b4d9e1a7c236
Revises: a2f7c5e9d148
Create Date: 2026-07-30

Two columns on `instructions`.

`embedding_vector` holds the same numbers as the existing JSON `embedding`, in
a type Postgres can search: pgvector then ranks the whole table in the database
instead of handing every row to Python. The JSON column stays the portable
source of truth, because SQLite has no vector type and keeps ranking in
process.

It is declared WITHOUT a dimension. `vector(1024)` would nail today's embedding
model into the schema, and swapping the model would mean a migration plus a
rewrite of every row before search worked again. Unsized, the column accepts
whatever the configured model produces; queries filter on `vector_dims` so two
sizes can coexist while a model change is being absorbed. The price is that no
HNSW index can be built on it ("column does not have dimensions") — an exact
scan in C is far past what this corpus needs, and an index for one specific
dimension can be added later without touching the schema.

`embedding_model` records which model produced the vector. Without it a model
change is silent and permanent damage: the re-embed sweep only ever looked for
EMPTY embeddings, and `rank` scores a vector of the wrong dimension 0, so every
record that existed before the change would score 0 forever, reachable only by
an exact title match. With the column, the startup sweep can see that a record
is stale and re-embed it.

The backfill copies existing vectors across but deliberately leaves
`embedding_model` NULL: nothing here knows which model wrote them, and claiming
otherwise would defeat the column's purpose. NULL reads as "unknown, therefore
stale", so the first startup after this migration re-embeds the table once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "b4d9e1a7c236"
down_revision: str | None = "a2f7c5e9d148"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POSTGRESQL = "postgresql"
TABLE = "instructions"


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == POSTGRESQL
    # VECTOR where it means something, JSON elsewhere: the column exists on both
    # dialects so the ORM model stays single, but only Postgres ever reads it.
    vector_type = Vector() if postgres else sa.JSON()
    op.add_column(TABLE, sa.Column("embedding_vector", vector_type, nullable=True))
    op.add_column(TABLE, sa.Column("embedding_model", sa.String(), nullable=True))
    if postgres:
        _backfill_vectors()


def downgrade() -> None:
    op.drop_column(TABLE, "embedding_model")
    op.drop_column(TABLE, "embedding_vector")


def _backfill_vectors() -> None:
    """Copy the JSON embeddings into the vector column.

    The cast goes through text because that is exactly pgvector's input format:
    a JSON array of numbers renders as `[0.1,0.2,...]`, which is what
    `::vector` parses. Rows with an empty embedding are skipped — they stay
    NULL and the startup sweep picks them up as it always has.
    """
    op.execute(
        sa.text(
            f"UPDATE {TABLE} SET embedding_vector = (embedding #>> '{{}}')::vector "
            "WHERE json_array_length(embedding) > 0"
        )
    )
