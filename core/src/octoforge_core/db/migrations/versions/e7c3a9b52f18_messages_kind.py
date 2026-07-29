"""messages.kind — the user's own words vs forwarded material

Revision ID: e7c3a9b52f18
Revises: d4b8f1c6e250
Create Date: 2026-07-29

Forwarded messages are content the user shared, not a question addressed to
the agent. NULL means "the user's own words", so every existing row keeps its
meaning without a backfill; only the exceptional kind is written.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a9b52f18"
down_revision: str | None = "d4b8f1c6e250"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("kind", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "kind")
