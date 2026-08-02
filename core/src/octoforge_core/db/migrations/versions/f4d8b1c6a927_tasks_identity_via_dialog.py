"""tasks loses user_id/channel: identity is reached through the dialog

Revision ID: f4d8b1c6a927
Revises: e2c7a4f9b581
Create Date: 2026-08-02

Both columns were copies taken from the dialog at task creation, "for the
sweeps" — but no query ever filtered tasks by either. The user_id copy even
kept an index that nothing read, maintained on an INSERT that sits on the
answer path. Every other table reaches identity through `dialog_id`; now
tasks does too, and the operator listing joins `dialogs` the same way the
exchanges listing always has.

Nothing to backfill: `dialog_id` was always there and always correct.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4d8b1c6a927"
down_revision: str | None = "e2c7a4f9b581"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_tasks_user_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("user_id")
        batch.drop_column("channel")


def downgrade() -> None:
    # restored empty; the pre-drop code fills them on the next task it creates,
    # and nothing reads historical values
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("user_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("channel", sa.String(), nullable=True))
    op.create_index("ix_tasks_user_id", "tasks", ["user_id"])
