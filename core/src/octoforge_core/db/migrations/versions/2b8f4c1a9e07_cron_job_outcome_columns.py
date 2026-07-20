"""cron job outcome columns

Adds the fire-outcome bookkeeping to cron_jobs: last_status/last_error of the
most recent fired process, the retry streak counter and the one_shot flag
(single-fire reminders deleted after the first success).

Column adds are conditional: a pre-Alembic database is adopted at the baseline
revision even when it was created by a newer `create_all` that already had
these columns, so the upgrade must not fail on duplicates.

Revision ID: 2b8f4c1a9e07
Revises: 675056c8fffd
Create Date: 2026-07-20 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b8f4c1a9e07'
down_revision: Union[str, None] = '675056c8fffd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OUTCOME_COLUMNS: tuple[str, ...] = ('one_shot', 'last_status', 'last_error', 'retry_count')


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('cron_jobs')}
    with op.batch_alter_table('cron_jobs', schema=None) as batch_op:
        if 'one_shot' not in existing:
            batch_op.add_column(
                sa.Column('one_shot', sa.Boolean(), server_default=sa.text('0'), nullable=False)
            )
        if 'last_status' not in existing:
            batch_op.add_column(sa.Column('last_status', sa.String(), nullable=True))
        if 'last_error' not in existing:
            batch_op.add_column(sa.Column('last_error', sa.String(), nullable=True))
        if 'retry_count' not in existing:
            batch_op.add_column(
                sa.Column('retry_count', sa.Integer(), server_default=sa.text('0'), nullable=False)
            )


def downgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('cron_jobs')}
    with op.batch_alter_table('cron_jobs', schema=None) as batch_op:
        for name in reversed(_OUTCOME_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
