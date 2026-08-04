"""user_params: per-user non-secret values substituted into endpoint calls

Deterministic values a record template references as `{user.code}` (timezone,
account ids, calendar paths): stored plaintext, set by the operator in the
admin console, substituted by the call executor so they never have to travel
through the model's context.

Table creation is conditional: a pre-Alembic database is adopted at the
baseline revision even when a newer `create_all` already made this table,
so the upgrade must not fail on duplicates.

Revision ID: f1b9d4e8c257
Revises: e4a7c2f9b361
Create Date: 2026-08-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1b9d4e8c257"
down_revision: str | None = "e4a7c2f9b361"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("user_params"):
        op.create_table(
            "user_params",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("code", sa.String(), nullable=False),
            sa.Column("value", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "code"),
        )
        op.create_index("ix_user_params_user_id", "user_params", ["user_id"], unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("user_params"):
        op.drop_index("ix_user_params_user_id", "user_params")
        op.drop_table("user_params")
