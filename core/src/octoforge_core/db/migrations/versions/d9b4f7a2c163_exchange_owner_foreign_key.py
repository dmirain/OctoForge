"""exchanges.owner_task_id becomes a real foreign key

Revision ID: d9b4f7a2c163
Revises: c8a3e5f1b729
Create Date: 2026-08-02

The column held the identity of the run currently answering an exchange, as a
plain string. `_settle_exchange` compares it against the settling task to
decide whether the exchange changed hands — an integrity rule the database
could not see, on a column it could not check.

`ON DELETE SET NULL` also replaces a rule the code was not enforcing: deleting
a task left the exchange pointing at a row that no longer exists. It is freed
now, which is what "nobody is working on this" already meant everywhere else.

Any value that does not resolve is cleared first. It could only be a leftover
of a deleted task, and the constraint would refuse it — leaving the exchange
unowned is exactly what such a row means.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9b4f7a2c163"
down_revision: str | None = "c8a3e5f1b729"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _clear_dangling_owners()
    # batch mode for SQLite, which cannot ALTER a constraint in; on Postgres
    # it is a plain ALTER TABLE ADD CONSTRAINT
    with op.batch_alter_table("exchanges") as batch:
        batch.create_foreign_key(
            "fk_exchanges_owner_task_id",
            "tasks",
            ["owner_task_id"],
            ["id"],
            ondelete="SET NULL",
        )


def _clear_dangling_owners() -> None:
    """Null every owner that no longer resolves to a task row."""
    op.execute(
        sa.text(
            "update exchanges set owner_task_id = null "
            "where owner_task_id is not null "
            "and owner_task_id not in (select id from tasks)"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("exchanges") as batch:
        batch.drop_constraint("fk_exchanges_owner_task_id", type_="foreignkey")
