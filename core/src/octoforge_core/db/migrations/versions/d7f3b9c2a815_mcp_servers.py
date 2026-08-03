"""mcp_servers and mcp_server_links: external MCP servers as shared records

Revision ID: d7f3b9c2a815
Revises: c2d8f5a1b476
Create Date: 2026-08-03

An MCP server added by anyone is a capability of the whole installation:
one row per URL, and a link row per person who added it. The server's tools
are mirrored into ordinary public endpoint records — no table of their own.

Table creation is conditional: a pre-Alembic database is adopted at the
baseline revision even when a newer `create_all` already made these tables,
so the upgrade must not fail on duplicates.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7f3b9c2a815"
down_revision: str | None = "c2d8f5a1b476"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("mcp_servers"):
        op.create_table(
            "mcp_servers",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("url", sa.String(), nullable=False),
            sa.Column("auth_secret_code", sa.String(), nullable=True),
            sa.Column("auth_header", sa.String(), nullable=False),
            sa.Column("auth_format", sa.String(), nullable=False),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_sync_error", sa.String(), nullable=True),
            sa.Column("tools_fingerprint", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            # the deduplication the table exists for: one row per server
            sa.UniqueConstraint("url"),
            # mirror records address the server by name
            sa.UniqueConstraint("name"),
        )
    if not inspector.has_table("mcp_server_links"):
        op.create_table(
            "mcp_server_links",
            sa.Column("server_id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["server_id"], ["mcp_servers.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("server_id", "user_id"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("mcp_server_links"):
        op.drop_table("mcp_server_links")
    if inspector.has_table("mcp_servers"):
        op.drop_table("mcp_servers")
