"""exchanges loses owner_task_id: ownership is derived from tasks

Revision ID: e2c7a4f9b581
Revises: d9b4f7a2c163
Create Date: 2026-08-02

The column was a pointer to the run answering the exchange, kept true by hand
at every start, reopen and cancel. `tasks.exchange_id` records the same fact
in the direction the schema runs, so the pointer was a second copy of an
answer the database already had:

- "is anybody working on it" = the exchange has a task in PENDING/RUNNING;
- "does this settle still apply" = the settling task is the exchange's newest
  one and the exchange is still live.

Dropping it also removes the exchanges<->tasks foreign-key cycle the pointer
forced (each table naming the other), which needed `use_alter` and made every
schema tool walk on eggshells. The schema is a tree rooted in dialogs again.

Nothing to backfill in either direction: the column's information is already
in `tasks`, put there by the same code paths that maintained the pointer.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2c7a4f9b581"
down_revision: str | None = "d9b4f7a2c163"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("exchanges") as batch:
        batch.drop_constraint("fk_exchanges_owner_task_id", type_="foreignkey")
        batch.drop_column("owner_task_id")


def downgrade() -> None:
    # the column comes back empty; ownership derives from tasks either way,
    # and pre-drop code repopulates it on the next run it starts
    with op.batch_alter_table("exchanges") as batch:
        batch.add_column(sa.Column("owner_task_id", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_exchanges_owner_task_id", "tasks", ["owner_task_id"], ["id"], ondelete="SET NULL"
        )
