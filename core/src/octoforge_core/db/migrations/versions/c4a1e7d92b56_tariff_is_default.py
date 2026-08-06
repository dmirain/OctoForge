"""tariff_is_default: the freemium default plan flag

One boolean on `tariffs`: the plan every user with no explicit binding
falls back to. At most one row carries the flag — an application-level
invariant kept by the store on every put, not a constraint, because a
partial unique index over a boolean is not dialect-neutral. Existing rows
get `false`: an installation that never marks a default keeps its
"no binding = unlimited" behavior unchanged.

The column add is conditional: a fresh non-SQLite database is built by
`create_all` + `stamp head`, and a pre-Alembic database adopted at the
baseline may already carry the model's schema.

Revision ID: c4a1e7d92b56
Revises: 78f3d64ee031
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a1e7d92b56"
down_revision: str | None = "78f3d64ee031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tariffs") and "is_default" not in _column_names(inspector, "tariffs"):
        op.add_column(
            "tariffs",
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tariffs") and "is_default" in _column_names(inspector, "tariffs"):
        op.drop_column("tariffs", "is_default")
