"""tasks.exchange_id: the obligation a run is paying, as a column

Revision ID: c8a3e5f1b729
Revises: b6d4f2a91c85
Create Date: 2026-08-02

Which exchange an ANSWER task serves was kept inside the task's `input` JSON
and dug out with a helper. Nothing could join on it, index it or check it,
and "the value in there is not a string" was a case the code had to carry.

The column is nullable because RUN tasks (cron, spawned work) genuinely have
no exchange: they owe the user nothing. Backfilled from the JSON, which is
left untouched — it is the run's input, and rewriting inputs after the fact
is not something a migration should do.

Two composite indexes come with it, for the two questions asked per turn:
"what is this exchange's work" and "what did this dialog leave stranded".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8a3e5f1b729"
down_revision: str | None = "b6d4f2a91c85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Enough of the table to read the inputs and write the new column. Declared
#: here rather than imported: a migration must keep working when the model
#: moves on.
_tasks = sa.table(
    "tasks",
    sa.column("id", sa.String()),
    sa.column("input", sa.JSON()),
    sa.column("exchange_id", sa.String()),
)


def upgrade() -> None:
    # batch mode because of SQLite: it cannot ALTER in a constraint, and the
    # constraint is the point — an untyped string column is what we already
    # have on the other side of this link and exactly what we are moving away
    # from. On Postgres batch mode is a plain ALTER.
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("exchange_id", sa.String(), nullable=True))
        batch.create_foreign_key("fk_tasks_exchange_id", "exchanges", ["exchange_id"], ["id"])
    _backfill_from_input()
    op.create_index("ix_tasks_exchange_status", "tasks", ["exchange_id", "status"])
    op.create_index("ix_tasks_dialog_status", "tasks", ["dialog_id", "status"])


def _backfill_from_input() -> None:
    """Copy `input['exchange_id']` into the column, row by row.

    In Python rather than in SQL: reading a key out of a JSON column is
    spelled differently in every dialect, and this table is small (it holds
    one row per run, and runs are deleted only administratively).
    """
    connection = op.get_bind()
    rows = connection.execute(sa.select(_tasks.c.id, _tasks.c.input)).all()
    updates = [
        {"row_id": task_id, "exchange": payload["exchange_id"]}
        for task_id, payload in rows
        if isinstance(payload, dict) and isinstance(payload.get("exchange_id"), str)
    ]
    if not updates:
        return
    connection.execute(
        _tasks.update().where(_tasks.c.id == sa.bindparam("row_id")).values(
            exchange_id=sa.bindparam("exchange")
        ),
        updates,
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_dialog_status", table_name="tasks")
    op.drop_index("ix_tasks_exchange_status", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("fk_tasks_exchange_id", type_="foreignkey")
        batch.drop_column("exchange_id")
