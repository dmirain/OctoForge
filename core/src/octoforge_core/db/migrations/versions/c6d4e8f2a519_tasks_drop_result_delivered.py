"""tasks drop result_delivered

Terminal task rows are now deleted outright (delivery deletes the row), so the
delivery flag is gone from both the domain object and the table.

The column drop is conditional: a database rebuilt by `create_all` from the new
models never had the column, so the upgrade must not fail on its absence.

Revision ID: c6d4e8f2a519
Revises: b3e7a91d4c05
Create Date: 2026-07-22 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c6d4e8f2a519'
down_revision: Union[str, None] = 'b3e7a91d4c05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('tasks')}
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        if 'result_delivered' in existing:
            batch_op.drop_column('result_delivered')


def downgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('tasks')}
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        if 'result_delivered' not in existing:
            batch_op.add_column(
                sa.Column('result_delivered', sa.Boolean(), server_default=sa.text('0'),
                          nullable=False)
            )
