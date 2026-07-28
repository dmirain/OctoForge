"""exchanges: the dialog obligation model

Revision ID: b7d1c4e8f062
Revises: a9c4e7f2b153
Create Date: 2026-07-28

An exchange is one obligation to the user: their question, its clarifications
and its answer. Legacy messages keep exchange_id NULL and render as plain
history — no backfill, so the startup sweep never resurrects old questions.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d1c4e8f062"
down_revision: str | None = "a9c4e7f2b153"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exchanges",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("dialog_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("owner_task_id", sa.String(), nullable=True),
        sa.Column("pending_question", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_exchanges_dialog_id", "exchanges", ["dialog_id"])
    op.create_index("ix_exchanges_status", "exchanges", ["status"])
    op.add_column("messages", sa.Column("exchange_id", sa.String(), nullable=True))
    op.create_index("ix_messages_exchange_id", "messages", ["exchange_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_exchange_id", table_name="messages")
    op.drop_column("messages", "exchange_id")
    op.drop_index("ix_exchanges_status", table_name="exchanges")
    op.drop_index("ix_exchanges_dialog_id", table_name="exchanges")
    op.drop_table("exchanges")
