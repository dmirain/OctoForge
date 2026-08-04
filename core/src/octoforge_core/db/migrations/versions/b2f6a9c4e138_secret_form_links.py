"""secret_form_links: short capability codes for the secrets form

The stateless token they replace was ~700 characters, and the agent had to
transcribe it into a chat message verbatim — which is what a model does
badly. The code is now short and the payload lives here.

Table creation is conditional: a pre-Alembic database is adopted at the
baseline revision even when a newer `create_all` already made this table,
so the upgrade must not fail on duplicates.

Revision ID: b2f6a9c4e138
Revises: a1c8e5f3b972
Create Date: 2026-08-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f6a9c4e138"
down_revision: str | None = "a1c8e5f3b972"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("secret_form_links"):
        op.create_table(
            "secret_form_links",
            sa.Column("code", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("prefill", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("code"),
        )
        op.create_index(
            "ix_secret_form_links_user_id", "secret_form_links", ["user_id"], unique=False
        )
        op.create_index(
            "ix_secret_form_links_expires_at", "secret_form_links", ["expires_at"], unique=False
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("secret_form_links"):
        op.drop_index("ix_secret_form_links_expires_at", "secret_form_links")
        op.drop_index("ix_secret_form_links_user_id", "secret_form_links")
        op.drop_table("secret_form_links")
