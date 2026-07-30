"""indexes for the two queries that scan a growing table

Revision ID: f3b8d2c5a714
Revises: e8c1b6d4a903
Create Date: 2026-07-30

Two places where the cost grows with data that is never deleted.

`datasets_query` filters `dataset_records` by a date range and orders by
`created_at`, with only `dataset_id` indexed — so the more history a dataset
accumulates, the more rows every query sorts. The composite covers both halves.

`list_undelivered` runs at startup over `tasks`, whose DONE branch grows without
bound by design. A partial index on the undelivered rows keeps that sweep
proportional to what is actually pending rather than to everything that ever
completed; the predicate has to be spelled per dialect or SQLite silently builds
a full index (the pattern comes from f2a6c8d1e935).

Deliberately not adding `dialogs.channel`: it is filtered in two places, but the
table holds one row per user per channel and the planner will keep choosing a
sequential scan over it for a long time. An index nobody uses still costs every
write.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3b8d2c5a714"
down_revision: str | None = "e8c1b6d4a903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNDELIVERED_PREDICATE = "delivered_at IS NULL"


def upgrade() -> None:
    op.create_index(
        "ix_dataset_records_dataset_created",
        "dataset_records",
        ["dataset_id", "created_at"],
    )
    op.create_index(
        "ix_tasks_undelivered",
        "tasks",
        ["status"],
        sqlite_where=sa.text(UNDELIVERED_PREDICATE),
        postgresql_where=sa.text(UNDELIVERED_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_undelivered", table_name="tasks")
    op.drop_index("ix_dataset_records_dataset_created", table_name="dataset_records")
