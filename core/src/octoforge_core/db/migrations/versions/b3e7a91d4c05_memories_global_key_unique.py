"""memories: partial unique index for global entries

Global memories (user_id IS NULL) were unique only by the store's
find-then-insert, which races under concurrency and yields duplicate rows.
This deduplicates existing global rows (keeping the newest per key) and adds
a partial unique index so the database itself enforces one row per global
key; the store resolves a lost race into an update on the IntegrityError.

The index add is conditional: a pre-Alembic database is adopted at the
baseline revision even when it was created by a newer `create_all` that
already had this index, so the upgrade must not fail on duplicates.

Revision ID: b3e7a91d4c05
Revises: 8a1f3d5c2e97
Create Date: 2026-07-22 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e7a91d4c05'
down_revision: Union[str, None] = '8a1f3d5c2e97'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = 'uq_memories_global_key'


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            DELETE FROM memories
            WHERE user_id IS NULL AND id NOT IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY key ORDER BY updated_at DESC, id
                           ) AS rn
                    FROM memories
                    WHERE user_id IS NULL
                ) ranked
                WHERE ranked.rn = 1
            )
            """
        )
    )
    existing = {index['name'] for index in sa.inspect(bind).get_indexes('memories')}
    if INDEX_NAME not in existing:
        op.create_index(
            INDEX_NAME,
            'memories',
            ['key'],
            unique=True,
            sqlite_where=sa.text('user_id IS NULL'),
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = {index['name'] for index in sa.inspect(bind).get_indexes('memories')}
    if INDEX_NAME in existing:
        op.drop_index(INDEX_NAME, table_name='memories')
