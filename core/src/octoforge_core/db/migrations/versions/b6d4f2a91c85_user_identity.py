"""users and user_identities: a person, independent of where they talk from

Revision ID: b6d4f2a91c85
Revises: a4e9c2b7f513
Create Date: 2026-08-01

A user used to *be* their transport handle. `tg:12345` was who they were and
where to write to them at once, which made changing a Telegram account
impossible: everything a person owned was filed under the old handle, with
nowhere to record that the new one is the same person.

This gives every person an id of their own and records what each surface
calls them. Re-seating then moves an identity rather than a user, and
everything filed under the core id follows without being touched.

The new ids are opaque, and every existing row is renumbered onto them.
Reusing the old strings would have been cheaper and would have kept the
disease: an id that can be parsed will be parsed — the delivery address used
to be read straight back out of it — so the ids stop carrying structure at
all.

Parsing `tg:` here is deliberate. It is a historical fact about how ids used
to be minted, which is exactly what a migration is for; no code above may
know it.
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6d4f2a91c85"
down_revision: str | None = "a4e9c2b7f513"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every column that named a user by their transport handle.
OWNER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("dialogs", "user_id"),
    ("tasks", "user_id"),
    ("cron_jobs", "user_id"),
    ("secrets", "user_id"),
    ("datasets", "owner_user_id"),
    ("dataset_records", "owner_user_id"),
    ("instructions", "owner_id"),
)

#: How Telegram ids used to be written. Historical, and confined to this file.
TELEGRAM_PREFIX = "tg:"
TELEGRAM_SURFACE = "telegram"
WEB_SURFACE = "web"

users = sa.table(
    "users",
    sa.column("id", sa.String),
    sa.column("email", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
identities = sa.table(
    "user_identities",
    sa.column("id", sa.String),
    sa.column("user_id", sa.String),
    sa.column("surface", sa.String),
    sa.column("external_id", sa.String),
    sa.column("details", sa.JSON),
    sa.column("active", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    _create_tables()
    connection = op.get_bind()
    now = sa.func.now()
    mapping = {handle: uuid.uuid4().hex for handle in _existing_handles(connection)}
    for handle, user_id in mapping.items():
        surface, external_id = _split(handle)
        connection.execute(users.insert().values(id=user_id, email=None, created_at=now))
        connection.execute(
            identities.insert().values(
                id=uuid.uuid4().hex,
                user_id=user_id,
                surface=surface,
                external_id=external_id,
                details={},
                active=True,
                created_at=now,
                updated_at=now,
            )
        )
    _renumber(connection, mapping)


def _create_tables() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "user_identities",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("surface", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        # the one invariant this table exists for: an account belongs to at
        # most one person
        sa.UniqueConstraint("surface", "external_id", name="uq_identity_account"),
    )
    op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])


def _existing_handles(connection: sa.Connection) -> set[str]:
    """Every handle any table filed something under."""
    handles: set[str] = set()
    for table, column in OWNER_COLUMNS:
        rows = connection.execute(
            sa.text(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL")  # noqa: S608
        )
        handles |= {row[0] for row in rows if row[0]}
    return handles


def _split(handle: str) -> tuple[str, str]:
    """Which surface a handle came from, and what that surface called them."""
    if handle.startswith(TELEGRAM_PREFIX):
        return TELEGRAM_SURFACE, handle[len(TELEGRAM_PREFIX) :]
    return WEB_SURFACE, handle


def _renumber(connection: sa.Connection, mapping: dict[str, str]) -> None:
    """Point every owning row at the person instead of at the handle."""
    for table, column in OWNER_COLUMNS:
        for handle, user_id in mapping.items():
            connection.execute(
                sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),  # noqa: S608
                {"new": user_id, "old": handle},
            )


def downgrade() -> None:
    """Restore the handles, then drop the tables.

    Lossless for anything this migration created: the identity rows still say
    which handle each person came from. A user with several identities cannot
    be expressed by a handle, so the first one recorded wins — which is the
    state that existed before this migration anyway.
    """
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT user_id, surface, external_id FROM user_identities ORDER BY created_at, id"
        )
    )
    handles: dict[str, str] = {}
    for user_id, surface, external_id in rows:
        if user_id in handles:
            continue
        handles[user_id] = (
            f"{TELEGRAM_PREFIX}{external_id}" if surface == TELEGRAM_SURFACE else external_id
        )
    for table, column in OWNER_COLUMNS:
        for user_id, handle in handles.items():
            connection.execute(
                sa.text(f"UPDATE {table} SET {column} = :old WHERE {column} = :new"),  # noqa: S608
                {"old": handle, "new": user_id},
            )
    op.drop_index("ix_user_identities_user_id", table_name="user_identities")
    op.drop_table("user_identities")
    op.drop_table("users")
