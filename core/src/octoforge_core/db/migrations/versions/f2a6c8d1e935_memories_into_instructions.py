"""Fold the memories table into instructions as type='memory' records

Memory storage merges into the instruction store: a memory becomes a private
instruction record (title = the memory key, owner_id = the user, type
'memory'), sharing the table, the embeddings and the search machinery.
Migrated rows get an empty embedding — migrations run without an embedder;
the startup `reembed_missing` sweep computes the vectors on the next boot.
Legacy global memories (user_id NULL) become public records, preserving
their everyone-can-read visibility. The memories table is dropped.

The copy is guarded on the table's existence: a pre-Alembic database adopted
at the baseline may have been created by a `create_all` that no longer knows
the memories table.

Revision ID: f2a6c8d1e935
Revises: d1e5f9a3b247
Create Date: 2026-07-25 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a6c8d1e935'
down_revision: Union[str, None] = 'd1e5f9a3b247'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MEMORY_TYPE = 'memory'

memories = sa.table(
    'memories',
    sa.column('id', sa.String),
    sa.column('user_id', sa.String),
    sa.column('key', sa.String),
    sa.column('content', sa.String),
    sa.column('tags', sa.JSON),
    sa.column('created_at', sa.DateTime),
    sa.column('updated_at', sa.DateTime),
)

instructions = sa.table(
    'instructions',
    sa.column('id', sa.String),
    sa.column('type', sa.String),
    sa.column('title', sa.String),
    sa.column('content', sa.Text),
    sa.column('embedding', sa.JSON),
    sa.column('tags', sa.JSON),
    sa.column('version', sa.Integer),
    sa.column('usage_count', sa.Integer),
    sa.column('success_count', sa.Integer),
    sa.column('system', sa.Boolean),
    sa.column('owner_id', sa.String),
    sa.column('created_at', sa.DateTime),
    sa.column('updated_at', sa.DateTime),
)


def upgrade() -> None:
    bind = op.get_bind()
    if 'memories' not in sa.inspect(bind).get_table_names():
        return
    copy = sa.select(
        memories.c.id,
        sa.literal(MEMORY_TYPE),
        memories.c.key,
        memories.c.content,
        # no embedder at migration time: the startup sweep fills the vector
        sa.literal([], type_=sa.JSON()),
        memories.c.tags,
        sa.literal(1),
        sa.literal(0),
        sa.literal(0),
        sa.false(),
        memories.c.user_id,
        memories.c.created_at,
        memories.c.updated_at,
    )
    op.execute(
        instructions.insert().from_select(
            [
                'id',
                'type',
                'title',
                'content',
                'embedding',
                'tags',
                'version',
                'usage_count',
                'success_count',
                'system',
                'owner_id',
                'created_at',
                'updated_at',
            ],
            copy,
        )
    )
    op.drop_table('memories')


def downgrade() -> None:
    op.create_table(
        'memories',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=True),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('content', sa.String(), nullable=False),
        sa.Column('tags', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'key'),
    )
    op.create_index('ix_memories_user_id', 'memories', ['user_id'], unique=False)
    op.create_index(
        'uq_memories_global_key',
        'memories',
        ['key'],
        unique=True,
        sqlite_where=sa.text('user_id IS NULL'),
        postgresql_where=sa.text('user_id IS NULL'),
    )
    copy_back = sa.select(
        instructions.c.id,
        instructions.c.owner_id,
        instructions.c.title,
        instructions.c.content,
        instructions.c.tags,
        instructions.c.created_at,
        instructions.c.updated_at,
    ).where(instructions.c.type == MEMORY_TYPE)
    op.execute(
        memories.insert().from_select(
            ['id', 'user_id', 'key', 'content', 'tags', 'created_at', 'updated_at'],
            copy_back,
        )
    )
    op.execute(instructions.delete().where(instructions.c.type == MEMORY_TYPE))
