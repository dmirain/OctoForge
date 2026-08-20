"""http_collections: two fixed tables for structured HTTP responses

A collection is rows, not a table: this migration is the only DDL the feature
ever runs. `collections` holds one row per collection — its passport and the
derived schema as a JSON *value* — and `collection_records` one row per data
element, jsonb on Postgres so the query engine's `#>>` operators and numeric
casts apply. On SQLite the tables exist but stay unqueried: the engine is a
Postgres-only capability and the tools are simply not wired there.

Table creation is conditional for the same reason as 78f3d64ee031: a fresh
non-SQLite database is built by `create_all` + `stamp head`, so this body runs
only on upgraded databases and must not fail on tables that already exist.

Revision ID: c7a2e9f4b318
Revises: b6c39d5e0f27
Create Date: 2026-08-19

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7a2e9f4b318"
down_revision: str | None = "b6c39d5e0f27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("collections"):
        op.create_table(
            "collections",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("label", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("schema", _json(), nullable=False),
            sa.Column("envelope", _json(), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("pages_loaded", sa.Integer(), nullable=False),
            sa.Column("truncated", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_collections_owner_id", "collections", ["owner_id"])
        op.create_index("ix_collections_expires_at", "collections", ["expires_at"])
    if not inspector.has_table("collection_records"):
        op.create_table(
            "collection_records",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("collection_id", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("payload", _json(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
        )
        op.create_index(
            "ix_collection_records_collection_id", "collection_records", ["collection_id"]
        )
        op.create_index(
            "ix_collection_records_collection_position",
            "collection_records",
            ["collection_id", "position"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("collection_records"):
        op.drop_table("collection_records")
    if inspector.has_table("collections"):
        op.drop_table("collections")
