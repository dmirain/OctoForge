"""tariff_max_memory_chars: cap the total size of a user's stored memories

One nullable integer on `tariffs`: the SUM of the user's memory contents in
characters that `memory_store` may not exceed. NULL keeps the dimension
unlimited, like every other cap.

The column add is conditional: a fresh non-SQLite database is built by
`create_all` + `stamp head`, and a pre-Alembic database adopted at the
baseline may already carry the model's schema.

Revision ID: a7f2d84c61b9
Revises: e9b3c5f7a814
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7f2d84c61b9"
down_revision: str | None = "e9b3c5f7a814"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tariffs") and "max_memory_chars" not in _column_names(
        inspector, "tariffs"
    ):
        op.add_column("tariffs", sa.Column("max_memory_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tariffs") and "max_memory_chars" in _column_names(
        inspector, "tariffs"
    ):
        op.drop_column("tariffs", "max_memory_chars")
