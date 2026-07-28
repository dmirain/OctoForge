"""instructions.author_id — authorship survives publication

Revision ID: d4b8f1c6e250
Revises: c9f2e4a7b381
Create Date: 2026-07-28

Publication used to clear owner_id and with it any trace of who wrote the
record. author_id is orthogonal to visibility: set at save time, carried
through publish, and it grants the author edit rights over their published
skill. Backfill: every privately-owned record's author is its owner;
already-published rows keep NULL (their history is gone — future publishes
copy the departing owner into author_id instead).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b8f1c6e250"
down_revision: str | None = "c9f2e4a7b381"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instructions", sa.Column("author_id", sa.String(), nullable=True))
    op.create_index("ix_instructions_author_id", "instructions", ["author_id"])
    op.execute("UPDATE instructions SET author_id = owner_id WHERE owner_id IS NOT NULL")


def downgrade() -> None:
    op.drop_index("ix_instructions_author_id", table_name="instructions")
    op.drop_column("instructions", "author_id")
