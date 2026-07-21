"""messages client_message_id

Adds the idempotency key of client-submitted messages: a nullable
client_message_id column with a unique (dialog_id, client_message_id)
constraint (SQLite lets multiple NULLs through, so keyed and unkeyed
messages coexist). Repeated submits with an already-seen key are skipped
by the actor; the constraint is the race-condition backstop.

The column add and the constraint are conditional: a pre-Alembic database
is adopted at the baseline revision even when it was created by a newer
`create_all` that already had them, so the upgrade must not fail on
duplicates.

Revision ID: 8a1f3d5c2e97
Revises: 4c7e2b9a1f63
Create Date: 2026-07-21 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8a1f3d5c2e97'
down_revision: Union[str, None] = '4c7e2b9a1f63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT_NAME = 'uq_messages_dialog_client_message'


def upgrade() -> None:
    bind = op.get_bind()
    existing_columns = {c['name'] for c in sa.inspect(bind).get_columns('messages')}
    existing_constraints = {
        c['name'] for c in sa.inspect(bind).get_unique_constraints('messages')
    }
    with op.batch_alter_table('messages', schema=None) as batch_op:
        if 'client_message_id' not in existing_columns:
            batch_op.add_column(sa.Column('client_message_id', sa.String(), nullable=True))
        if _CONSTRAINT_NAME not in existing_constraints:
            batch_op.create_unique_constraint(
                _CONSTRAINT_NAME, ['dialog_id', 'client_message_id']
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing_columns = {c['name'] for c in sa.inspect(bind).get_columns('messages')}
    existing_constraints = {
        c['name'] for c in sa.inspect(bind).get_unique_constraints('messages')
    }
    with op.batch_alter_table('messages', schema=None) as batch_op:
        if _CONSTRAINT_NAME in existing_constraints:
            batch_op.drop_constraint(_CONSTRAINT_NAME, type_='unique')
        if 'client_message_id' in existing_columns:
            batch_op.drop_column('client_message_id')
