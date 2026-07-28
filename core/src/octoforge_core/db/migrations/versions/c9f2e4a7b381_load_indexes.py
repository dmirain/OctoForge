"""indexes for the hot queries the load audit flagged

Revision ID: c9f2e4a7b381
Revises: b7d1c4e8f062
Create Date: 2026-07-28

`list_live` runs on every message and every loop iteration and filters by
(dialog_id, status): without the composite index its cost grows with the
dialog's TOTAL exchange count, terminal rows included. `tasks.status` backs
the startup sweeps (`list_orphaned`, `list_undelivered`) over a table that
by design never deletes rows — a full scan there gates every deploy's
readiness.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9f2e4a7b381"
down_revision: str | None = "b7d1c4e8f062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_exchanges_dialog_status", "exchanges", ["dialog_id", "status"])
    op.create_index("ix_tasks_status", "tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_exchanges_dialog_status", table_name="exchanges")
