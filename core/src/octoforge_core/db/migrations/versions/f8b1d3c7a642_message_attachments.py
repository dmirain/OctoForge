"""messages.attachments — files that came with a message, as references

Revision ID: f8b1d3c7a642
Revises: e7c3a9b52f18
Create Date: 2026-07-29

Images are understood by a separate vision model and enter the dialog as
text; the reference is kept so a tool can look at the same picture again.
Bytes are never stored — Telegram keeps a bot's files, so `tg:<file_id>`
is enough. NULL means the message carried no files.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8b1d3c7a642"
down_revision: str | None = "e7c3a9b52f18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("attachments", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "attachments")
