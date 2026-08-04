"""secrets metadata columns: description, placements, transform

A secret gains a human/LLM-facing purpose (`description`), an opt-in list of
request parts it may be substituted into (`placements`, comma-joined; NULL
means the historical default — headers only), and an optional static
transform applied to the value before substitution (`transform`, e.g.
`base64` for HTTP Basic).

Column adds are conditional: a pre-Alembic database is adopted at the
baseline revision even when it was created by a newer `create_all` that
already had these columns, so the upgrade must not fail on duplicates.

Revision ID: e4a7c2f9b361
Revises: d7f3b9c2a815
Create Date: 2026-08-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a7c2f9b361"
down_revision: str | None = "d7f3b9c2a815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METADATA_COLUMNS: tuple[str, ...] = ("description", "placements", "transform")


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("secrets")}
    with op.batch_alter_table("secrets", schema=None) as batch_op:
        for name in _METADATA_COLUMNS:
            if name not in existing:
                batch_op.add_column(sa.Column(name, sa.String(), nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("secrets")}
    with op.batch_alter_table("secrets", schema=None) as batch_op:
        for name in reversed(_METADATA_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
