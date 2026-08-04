"""secrets.description becomes required; legacy rows are backfilled

Rows stored before the description existed get a placeholder that itself
tells the agent what to do — ask the user and update via secret_link — so
the "undocumented secret" state lives in the data, not in special-casing
around a NULL.

The backfill and the NOT NULL tightening are conditional for the usual
reason: a pre-Alembic database adopted at the baseline may have been created
by a newer `create_all` where the column is already non-nullable.

Revision ID: a1c8e5f3b972
Revises: f1b9d4e8c257
Create Date: 2026-08-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c8e5f3b972"
down_revision: str | None = "f1b9d4e8c257"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL_DESCRIPTION = (
    "no description yet — ask the user what this secret is for and update it via secret_link"
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE secrets SET description = :text WHERE description IS NULL"),
        {"text": BACKFILL_DESCRIPTION},
    )
    columns = {c["name"]: c for c in sa.inspect(bind).get_columns("secrets")}
    if columns["description"]["nullable"]:
        with op.batch_alter_table("secrets", schema=None) as batch_op:
            batch_op.alter_column("description", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("secrets")}
    if not columns["description"]["nullable"]:
        with op.batch_alter_table("secrets", schema=None) as batch_op:
            batch_op.alter_column("description", existing_type=sa.String(), nullable=True)
