"""cron job outcome columns

Adds the fire-outcome bookkeeping to cron_jobs: last_status/last_error of the
most recent fired process, the retry streak counter and the one_shot flag
(single-fire reminders deleted after the first success).

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


def upgrade() -> None:
    with op.batch_alter_table('cron_jobs', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('one_shot', sa.Boolean(), server_default=sa.text('0'), nullable=False)
        )
        batch_op.add_column(sa.Column('last_status', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('last_error', sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column('retry_count', sa.Integer(), server_default=sa.text('0'), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table('cron_jobs', schema=None) as batch_op:
        batch_op.drop_column('retry_count')
        batch_op.drop_column('last_error')
        batch_op.drop_column('last_status')
        batch_op.drop_column('one_shot')
