"""instructions: per-user ownership (owner_id) with public/private uniqueness

Adds `owner_id` (NULL = public record, a user id = private to that owner) and
replaces the global UNIQUE(type, title) with UNIQUE(type, title, owner_id)
plus a partial unique index on (type, title) WHERE owner_id IS NULL (a plain
unique constraint cannot guard NULL owners). Existing rows become public
(owner_id NULL), so visibility semantics are unchanged for them.

The old UNIQUE(type, title) constraint is unnamed in pre-existing databases
(SQLite sqlite_autoindex), which Alembic batch mode cannot drop by name, so
the table is rebuilt with explicit SQL instead. The rebuild is conditional:
a pre-Alembic database adopted at the baseline may already carry the column
when it was created by a newer `create_all`.

Revision ID: e5f8a2c1d394
Revises: c6d4e8f2a519
Create Date: 2026-07-23 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f8a2c1d394'
down_revision: Union[str, None] = 'c6d4e8f2a519'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PUBLIC_INDEX_NAME = 'uq_instructions_public_type_title'
OWNER_INDEX_NAME = 'ix_instructions_owner_id'

COLUMN_NAMES = (
    'id', 'type', 'title', 'content', 'embedding', 'tags', 'version',
    'usage_count', 'success_count', 'created_at', 'updated_at', 'system',
)


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c['name'] for c in sa.inspect(bind).get_columns('instructions')}
    if 'owner_id' not in columns:
        bind.execute(
            sa.text(
                """
                CREATE TABLE instructions_new (
                    id VARCHAR NOT NULL,
                    type VARCHAR NOT NULL,
                    title VARCHAR NOT NULL,
                    content TEXT NOT NULL,
                    embedding JSON NOT NULL,
                    tags JSON NOT NULL,
                    version INTEGER NOT NULL,
                    usage_count INTEGER NOT NULL,
                    success_count INTEGER NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    system BOOLEAN DEFAULT 0 NOT NULL,
                    owner_id VARCHAR,
                    PRIMARY KEY (id),
                    CONSTRAINT uq_instructions_type_title_owner
                        UNIQUE (type, title, owner_id)
                )
                """
            )
        )
        bind.execute(
            sa.text(
                f"INSERT INTO instructions_new ({', '.join(COLUMN_NAMES)}, owner_id) "
                f"SELECT {', '.join(COLUMN_NAMES)}, NULL FROM instructions"
            )
        )
        bind.execute(sa.text("DROP TABLE instructions"))
        bind.execute(sa.text("ALTER TABLE instructions_new RENAME TO instructions"))
        bind.execute(sa.text("CREATE INDEX ix_instructions_type ON instructions (type)"))
        bind.execute(sa.text("CREATE INDEX ix_instructions_title ON instructions (title)"))
    _create_index_if_absent(bind, OWNER_INDEX_NAME, ['owner_id'], unique=False)
    _create_index_if_absent(
        bind,
        PUBLIC_INDEX_NAME,
        ['type', 'title'],
        unique=True,
        where='owner_id IS NULL',
    )


def downgrade() -> None:
    bind = op.get_bind()
    # private rows may duplicate (type, title) across owners; keep the newest
    bind.execute(
        sa.text(
            """
            DELETE FROM instructions
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY type, title
                               ORDER BY updated_at DESC, id
                           ) AS rn
                    FROM instructions
                ) ranked
                WHERE ranked.rn = 1
            )
            """
        )
    )
    bind.execute(
        sa.text(
            """
            CREATE TABLE instructions_old (
                id VARCHAR NOT NULL,
                type VARCHAR NOT NULL,
                title VARCHAR NOT NULL,
                content TEXT NOT NULL,
                embedding JSON NOT NULL,
                tags JSON NOT NULL,
                version INTEGER NOT NULL,
                usage_count INTEGER NOT NULL,
                success_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                system BOOLEAN DEFAULT 0 NOT NULL,
                PRIMARY KEY (id),
                UNIQUE (type, title)
            )
            """
        )
    )
    bind.execute(
        sa.text(
            f"INSERT INTO instructions_old ({', '.join(COLUMN_NAMES)}) "
            f"SELECT {', '.join(COLUMN_NAMES)} FROM instructions"
        )
    )
    bind.execute(sa.text("DROP TABLE instructions"))
    bind.execute(sa.text("ALTER TABLE instructions_old RENAME TO instructions"))
    bind.execute(sa.text("CREATE INDEX ix_instructions_type ON instructions (type)"))
    bind.execute(sa.text("CREATE INDEX ix_instructions_title ON instructions (title)"))


def _create_index_if_absent(
    bind: sa.engine.Connection,
    name: str,
    columns: list[str],
    *,
    unique: bool,
    where: str | None = None,
) -> None:
    existing = {index['name'] for index in sa.inspect(bind).get_indexes('instructions')}
    if name in existing:
        return
    statement = (
        f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} "
        f"ON instructions ({', '.join(columns)})"
    )
    if where is not None:
        statement += f" WHERE {where}"
    bind.execute(sa.text(statement))
