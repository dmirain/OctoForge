"""dialog claims: which process runs which actor

Revision ID: a4e9c2b7f513
Revises: f3b8d2c5a714
Create Date: 2026-07-31

One row per dialog, taken when a process builds the runner. Two questions
need answering once more than one process exists, and one column each
answers them: `generation` tells a previous owner it was preempted (it
compares the number it was born with against the stored one), and
`heartbeat_at` tells recovery whether an owner is still alive before it may
touch that dialog's stranded work.

Empty on upgrade. A dialog with no row has never been claimed and is free to
take, so an installation that never restarts behaves exactly as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4e9c2b7f513"
down_revision: str | None = "f3b8d2c5a714"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dialog_claims",
        sa.Column("dialog_id", sa.String(), nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dialog_id"], ["dialogs.id"]),
        sa.PrimaryKeyConstraint("dialog_id"),
    )
    # recovery filters by it over every dialog holding stranded work
    op.create_index("ix_dialog_claims_heartbeat_at", "dialog_claims", ["heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_dialog_claims_heartbeat_at", table_name="dialog_claims")
    op.drop_table("dialog_claims")
