"""messages.task_id + tasks.delivered_at: task-produced messages and delivery

Adds messages.task_id (the background task that produced an assistant
message; NULL for plain narrative messages) with its lookup index, and
tasks.delivered_at (when the terminal result reached the user transport;
NULL = still awaiting delivery). Task rows are no longer deleted on
delivery, so the delivery state needs an explicit column.

Column/index adds are conditional: a pre-Alembic database is adopted at the
baseline revision even when it was created by a newer `create_all` that
already had these columns, so the upgrade must not fail on duplicates.

Revision ID: d1e5f9a3b247
Revises: e5f8a2c1d394
Create Date: 2026-07-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e5f9a3b247'
down_revision: Union[str, None] = 'e5f8a2c1d394'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TASK_ID_INDEX = 'ix_messages_task_id'


def upgrade() -> None:
    bind = op.get_bind()
    message_columns = {c['name'] for c in sa.inspect(bind).get_columns('messages')}
    task_columns = {c['name'] for c in sa.inspect(bind).get_columns('tasks')}
    message_indexes = {i['name'] for i in sa.inspect(bind).get_indexes('messages')}
    with op.batch_alter_table('messages', schema=None) as batch_op:
        if 'task_id' not in message_columns:
            batch_op.add_column(sa.Column('task_id', sa.String(), nullable=True))
        if TASK_ID_INDEX not in message_indexes:
            batch_op.create_index(TASK_ID_INDEX, ['task_id'], unique=False)
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        if 'delivered_at' not in task_columns:
            batch_op.add_column(sa.Column('delivered_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    message_columns = {c['name'] for c in sa.inspect(bind).get_columns('messages')}
    task_columns = {c['name'] for c in sa.inspect(bind).get_columns('tasks')}
    message_indexes = {i['name'] for i in sa.inspect(bind).get_indexes('messages')}
    with op.batch_alter_table('messages', schema=None) as batch_op:
        if TASK_ID_INDEX in message_indexes:
            batch_op.drop_index(TASK_ID_INDEX)
        if 'task_id' in message_columns:
            batch_op.drop_column('task_id')
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        if 'delivered_at' in task_columns:
            batch_op.drop_column('delivered_at')
