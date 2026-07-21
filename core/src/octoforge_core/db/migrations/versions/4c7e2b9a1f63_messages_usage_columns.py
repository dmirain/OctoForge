"""messages usage columns

Adds provider token accounting to messages: prompt_tokens/completion_tokens
of the LLM call that produced an assistant message (NULL elsewhere). Feeds
the token-based compaction trigger and future per-user cost accounting.

Column adds are conditional: a pre-Alembic database is adopted at the baseline
revision even when it was created by a newer `create_all` that already had
these columns, so the upgrade must not fail on duplicates.

Revision ID: 4c7e2b9a1f63
Revises: 9d3c5f1a2b84
Create Date: 2026-07-21 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4c7e2b9a1f63'
down_revision: Union[str, None] = '9d3c5f1a2b84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_USAGE_COLUMNS: tuple[str, ...] = ('prompt_tokens', 'completion_tokens')


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('messages')}
    with op.batch_alter_table('messages', schema=None) as batch_op:
        for name in _USAGE_COLUMNS:
            if name not in existing:
                batch_op.add_column(sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('messages')}
    with op.batch_alter_table('messages', schema=None) as batch_op:
        for name in reversed(_USAGE_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
