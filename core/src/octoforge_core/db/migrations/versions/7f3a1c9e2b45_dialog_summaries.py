"""dialog summaries

Adds the dialog_summaries table of the context module: compressed segments of
a dialog's message archive (inclusive seq range, topic tags, content), written
by the background compactor.

The create is conditional: a pre-Alembic database is adopted at the baseline
revision even when it was created by a newer `create_all` that already had
this table, so the upgrade must not fail on duplicates.

Revision ID: 7f3a1c9e2b45
Revises: 2b8f4c1a9e07
Create Date: 2026-07-20 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

import octoforge_core.db.base


# revision identifiers, used by Alembic.
revision: str = '7f3a1c9e2b45'
down_revision: Union[str, None] = '2b8f4c1a9e07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'dialog_summaries'


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table('dialog_summaries',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('dialog_id', sa.String(), nullable=False),
    sa.Column('seq_from', sa.Integer(), nullable=False),
    sa.Column('seq_to', sa.Integer(), nullable=False),
    sa.Column('topics', sa.JSON(), nullable=False),
    sa.Column('content', sa.String(), nullable=False),
    sa.Column('created_at', octoforge_core.db.base.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['dialog_id'], ['dialogs.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('dialog_summaries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_dialog_summaries_dialog_id'), ['dialog_id'], unique=False)


def downgrade() -> None:
    if _TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    with op.batch_alter_table('dialog_summaries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_dialog_summaries_dialog_id'))

    op.drop_table('dialog_summaries')
