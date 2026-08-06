"""user_status_and_app_settings: admission statuses and operator settings

`users.status` (waiting/active/banned): everyone is born waiting and a free
slot under the operator's cap promotes them — but every EXISTING row is
backfilled `active`, because an upgrade must not lock the installation's
current people out. `app_settings` is the generic operator key→value table
(first key: `max_active_users`) — settings live in data so the console can
change them without a redeploy.

Both changes are conditional: a fresh non-SQLite database is built by
`create_all` + `stamp head`, and a pre-Alembic database adopted at the
baseline may already carry the model's schema.

Revision ID: e9b3c5f7a814
Revises: c4a1e7d92b56
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9b3c5f7a814"
down_revision: str | None = "c4a1e7d92b56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("users") and "status" not in _column_names(inspector, "users"):
        # server_default backfills the rows that predate statuses; the ORM
        # sets every new row explicitly (waiting), so the default never
        # decides a newcomer's fate
        op.add_column(
            "users",
            sa.Column("status", sa.String(), nullable=False, server_default="active"),
        )
    if not inspector.has_table("app_settings"):
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(), nullable=False),
            sa.Column("value", sa.String(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("app_settings"):
        op.drop_table("app_settings")
    if inspector.has_table("users") and "status" in _column_names(inspector, "users"):
        op.drop_column("users", "status")
