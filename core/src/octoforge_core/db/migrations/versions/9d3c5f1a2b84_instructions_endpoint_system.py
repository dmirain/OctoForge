"""instructions: endpoint type and system flag

Renames the instruction type 'tool' to 'endpoint' (data update of existing
records) and adds the `system` flag marking registry-owned records (upserted
and deleted by the startup registry sync only).

The column add is conditional: a pre-Alembic database is adopted at the
baseline revision even when it was created by a newer `create_all` that
already had this column, so the upgrade must not fail on duplicates.

Revision ID: 9d3c5f1a2b84
Revises: 7f3a1c9e2b45
Create Date: 2026-07-21 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d3c5f1a2b84'
down_revision: Union[str, None] = '7f3a1c9e2b45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c['name'] for c in sa.inspect(bind).get_columns('instructions')}
    if 'system' not in existing:
        with op.batch_alter_table('instructions', schema=None) as batch_op:
            batch_op.add_column(
                sa.Column('system', sa.Boolean(), server_default=sa.text('0'), nullable=False)
            )
    bind.execute(sa.text("UPDATE instructions SET type = 'endpoint' WHERE type = 'tool'"))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE instructions SET type = 'tool' WHERE type = 'endpoint'"))
    existing = {c['name'] for c in sa.inspect(bind).get_columns('instructions')}
    if 'system' in existing:
        with op.batch_alter_table('instructions', schema=None) as batch_op:
            batch_op.drop_column('system')
