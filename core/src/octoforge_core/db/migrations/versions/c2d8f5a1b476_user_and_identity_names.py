"""users.name and user_identities.name/username: what to call a person

Revision ID: c2d8f5a1b476
Revises: f4d8b1c6a927
Create Date: 2026-08-03

A person had an opaque id and nothing to address them by; every surface that
wanted to greet somebody had to reach into its own mirror tables. The name of
the person now lives on `users`, and what each surface currently calls them —
display name plus the optional handle — lives on their identity.

The split matters: `users.name` is the person's canonical name, seeded from a
surface once and thereafter theirs; `user_identities.name` is a living mirror
of the surface profile and may drift as they rename themselves there. A name
is always present (empty only until a surface reports one); a username is a
surface luxury and may be absent, which is why one column is NOT NULL and the
other nullable.

Column adds are conditional: a pre-Alembic database is adopted at the baseline
revision even when it was created by a newer `create_all` that already had
these columns, so the upgrade must not fail on duplicates.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d8f5a1b476"
down_revision: str | None = "f4d8b1c6a927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: table -> columns added there, in add order.
_COLUMNS: tuple[tuple[str, tuple[sa.Column[str], ...]], ...] = (
    ("users", (sa.Column("name", sa.String(), nullable=False, server_default=""),)),
    (
        "user_identities",
        (
            sa.Column("name", sa.String(), nullable=False, server_default=""),
            sa.Column("username", sa.String(), nullable=True),
        ),
    ),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, columns in _COLUMNS:
        existing = {column["name"] for column in inspector.get_columns(table)}
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column in columns:
                if column.name not in existing:
                    batch_op.add_column(column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, columns in _COLUMNS:
        existing = {column["name"] for column in inspector.get_columns(table)}
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column in reversed(columns):
                if column.name in existing:
                    batch_op.drop_column(column.name)
