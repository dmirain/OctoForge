"""Per-user encrypted secrets bound to a single allowed host

The secrets module: values encrypted at rest (Fernet), resolved only inside
the external-call executor; everything else sees codes and metadata. The
host binding is the exfiltration guard.

Revision ID: a9c4e7f2b153
Revises: f2a6c8d1e935
Create Date: 2026-07-26 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c4e7f2b153'
down_revision: Union[str, None] = 'f2a6c8d1e935'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'secrets',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('code', sa.String(), nullable=False),
        sa.Column('ciphertext', sa.String(), nullable=False),
        sa.Column('allowed_host', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'code'),
    )
    op.create_index('ix_secrets_user_id', 'secrets', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_secrets_user_id', 'secrets')
    op.drop_table('secrets')
