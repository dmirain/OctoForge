"""SQLAlchemy storage of the memory module (module-internal).

Follows the `datasets/store.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to DTOs at the boundary.

Uniqueness note: the (user_id, key) UniqueConstraint does NOT guard global
entries — both SQLite and PostgreSQL treat NULLs as distinct in unique
constraints, so duplicate (NULL, key) rows would be accepted. A partial
unique index (`user_id IS NULL`) enforces one row per global key instead.
The store still selects by owner first (comparing the NULL owner via
`user_id.is_(None)`, never `== None`), and a lost find-then-insert race is
redone as an update of the winner's row on the IntegrityError.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.memory.api import Memory, MemoryNotFoundError, MemoryStore
from octoforge_core.memory.models import MemoryRow
from octoforge_core.time import utc_now

LIKE_ESCAPE = "\\"


class SqlAlchemyMemoryStore(MemoryStore):
    """SQL persistence for memory entries."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def put(
        self,
        user_id: str | None,
        key: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> tuple[Memory, bool]:
        """Upsert by (user_id, key); see the port for the contract."""
        async with self._session_factory() as session:
            row = await _find_row(session, user_id, key)
            if row is None:
                row = MemoryRow(
                    id=uuid.uuid4().hex,
                    user_id=user_id,
                    key=key,
                    content=content,
                    tags=list(tags),
                )
                session.add(row)
                try:
                    await session.commit()
                except IntegrityError:
                    # lost the find-then-insert race with a concurrent writer
                    # (global keys hit the partial unique index, user keys the
                    # (user_id, key) constraint): redo as an update
                    await session.rollback()
                    winner = await _find_row(session, user_id, key)
                    if winner is None:  # the concurrent transaction rolled back too
                        raise
                    row = winner
                    row.content = content
                    row.tags = list(tags)
                    row.updated_at = utc_now()
                    await session.commit()
                    return _to_memory(row), False
                return _to_memory(row), True
            row.content = content
            row.tags = list(tags)
            row.updated_at = utc_now()
            await session.commit()
            return _to_memory(row), False

    async def get(self, user_id: str | None, key: str) -> Memory:
        """Return the memory of this owner by key; raise `MemoryNotFoundError`."""
        async with self._session_factory() as session:
            row = await _find_row(session, user_id, key)
            if row is None:
                raise MemoryNotFoundError(_not_found_message(user_id, key))
            return _to_memory(row)

    async def search(self, user_id: str, query: str, limit: int) -> list[Memory]:
        """Return visible memories matching the substring, newest first."""
        needle = query.strip()
        if not needle:
            return []
        pattern = f"%{_escape_like(needle)}%"
        async with self._session_factory() as session:
            statement = (
                select(MemoryRow)
                .where(
                    or_(MemoryRow.user_id == user_id, MemoryRow.user_id.is_(None)),
                    or_(
                        MemoryRow.key.ilike(pattern, escape=LIKE_ESCAPE),
                        MemoryRow.content.ilike(pattern, escape=LIKE_ESCAPE),
                    ),
                )
                .order_by(MemoryRow.updated_at.desc(), MemoryRow.key)
                .limit(limit)
            )
            rows = (await session.scalars(statement)).all()
            return [_to_memory(row) for row in rows]

    async def delete(self, user_id: str | None, key: str) -> None:
        """Delete the memory of this owner by key; raise `MemoryNotFoundError`."""
        async with self._session_factory() as session:
            row = await _find_row(session, user_id, key)
            if row is None:
                raise MemoryNotFoundError(_not_found_message(user_id, key))
            await session.delete(row)
            await session.commit()


async def _find_row(session: AsyncSession, user_id: str | None, key: str) -> MemoryRow | None:
    owner_clause = MemoryRow.user_id.is_(None) if user_id is None else MemoryRow.user_id == user_id
    result = await session.scalars(select(MemoryRow).where(owner_clause, MemoryRow.key == key))
    return result.first()


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so the query stays a literal substring."""
    return (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def _not_found_message(user_id: str | None, key: str) -> str:
    scope = "global" if user_id is None else f"user '{user_id}'"
    return f"memory '{key}' not found (scope: {scope})"


def _to_memory(row: MemoryRow) -> Memory:
    return Memory(
        id=row.id,
        user_id=row.user_id,
        key=row.key,
        content=row.content,
        tags=tuple(row.tags),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
