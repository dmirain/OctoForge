"""tariffs_and_usage: plan catalog, user bindings and the usage ledger

Three tables for per-user plans: `tariffs` (operator-defined plans — feature
codes plus nullable numeric caps, NULL = unlimited), `user_tariffs` (at most
one binding per user; no row = no restrictions) and `usage_events`, an
insert-only ledger of metered actions. The ledger is a log, not a counter:
limit checks and reports sum over a window through the composite
(user_id, created_at) index, and concurrent writers on two nodes never
contend. No seed rows on purpose — a fresh non-SQLite database is built by
`create_all` + `stamp head` and skips migration bodies, so seeded data would
exist only on upgraded databases.

Table creation is conditional: a pre-Alembic database is adopted at the
baseline revision even when a newer `create_all` already made these tables,
so the upgrade must not fail on duplicates.

Revision ID: 78f3d64ee031
Revises: b2f6a9c4e138
Create Date: 2026-08-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "78f3d64ee031"
down_revision: str | None = "b2f6a9c4e138"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tariffs"):
        op.create_table(
            "tariffs",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("code", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("features", sa.JSON(), nullable=False),
            sa.Column("daily_tokens", sa.Integer(), nullable=True),
            sa.Column("daily_user_messages", sa.Integer(), nullable=True),
            sa.Column("daily_assistant_messages", sa.Integer(), nullable=True),
            sa.Column("max_cron_jobs", sa.Integer(), nullable=True),
            sa.Column("max_datasets", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("code"),
        )
    if not inspector.has_table("user_tariffs"):
        op.create_table(
            "user_tariffs",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("tariff_id", sa.String(), nullable=False),
            sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_user_tariffs_user_id", "user_tariffs", ["user_id"], unique=True)
    if not inspector.has_table("usage_events"):
        op.create_table(
            "usage_events",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("origin", sa.String(), nullable=False),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False),
            sa.Column("completion_tokens", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("dialog_id", sa.String(), nullable=True),
            sa.Column("exchange_id", sa.String(), nullable=True),
            sa.Column("task_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_usage_events_user_created", "usage_events", ["user_id", "created_at"], unique=False
        )
        op.create_index("ix_usage_events_task_id", "usage_events", ["task_id"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("usage_events"):
        op.drop_index("ix_usage_events_task_id", "usage_events")
        op.drop_index("ix_usage_events_user_created", "usage_events")
        op.drop_table("usage_events")
    if inspector.has_table("user_tariffs"):
        op.drop_index("ix_user_tariffs_user_id", "user_tariffs")
        op.drop_table("user_tariffs")
    if inspector.has_table("tariffs"):
        op.drop_table("tariffs")
