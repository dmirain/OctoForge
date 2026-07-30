"""SQLAlchemy storage of the instructions module.

Follows the `db/repositories.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to facade DTOs at the boundary.
Implements the `InstructionStore` port from `instructions/api.py`; the
vector-search capability (`InstructionVectorSearch`) is deliberately not
implemented — this store ranks brute-force in the process.
"""

import asyncio
import uuid
from typing import Any, cast

from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionType,
)
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.time import utc_now

FIRST_VERSION = 1


class SqlAlchemyInstructionStore:
    """SQL persistence for instruction records and their embeddings."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(self, draft: InstructionDraft) -> Instruction:
        """Create the record or replace content/tags/embedding, bumping the version."""
        async with self._session_factory() as session:
            row = await self._find_row(session, draft.kind, draft.title, draft.owner_id)
            if row is not None:
                return await self._update_row(session, row, draft)
            try:
                return await self._insert_row(session, draft)
            except IntegrityError:
                # lost the find-then-insert race with a concurrent process
                # (e.g. web + standalone syncing the registry at startup over
                # one SQLite file): redo as an update of the winner's row
                await session.rollback()
                winner = await self._find_row(session, draft.kind, draft.title, draft.owner_id)
                if winner is None:  # the concurrent transaction rolled back too
                    raise
                return await self._update_row(session, winner, draft)

    async def _insert_row(self, session: AsyncSession, draft: InstructionDraft) -> Instruction:
        row = InstructionRow(
            id=uuid.uuid4().hex,
            type=draft.kind.value,
            title=draft.title,
            content=draft.content,
            embedding=list(draft.embedding),
            # NULL rather than an empty vector when the embedding failed:
            # pgvector cannot store a zero-dimension value, and NULL is what
            # `search_by_vector` skips
            embedding_vector=list(draft.embedding) or None,
            embedding_model=draft.embedding_model,
            tags=list(draft.tags),
            version=FIRST_VERSION,
            system=draft.system,
            owner_id=draft.owner_id,
            author_id=draft.author_id,
        )
        session.add(row)
        await session.commit()
        return to_instruction(row)

    async def _update_row(
        self,
        session: AsyncSession,
        row: InstructionRow,
        draft: InstructionDraft,
    ) -> Instruction:
        row.content = draft.content
        row.embedding = list(draft.embedding)
        row.embedding_vector = list(draft.embedding) or None
        row.embedding_model = draft.embedding_model
        row.tags = list(draft.tags)
        row.version += 1
        row.system = draft.system
        if draft.author_id is not None:
            # authorship is set, never cleared: a registry/system upsert
            # (author None) must not strip a record of its author
            row.author_id = draft.author_id
        row.updated_at = utc_now()
        await session.commit()
        return to_instruction(row)

    async def get_by_title(
        self,
        title: str,
        kind: InstructionType | None,
        owner_id: str | None = None,
    ) -> Instruction | None:
        """Return the record by (title, kind, owner), oldest first on collisions."""
        async with self._session_factory() as session:
            statement = select(InstructionRow).where(
                InstructionRow.title == title,
                _owner_clause(owner_id),
            )
            if kind is not None:
                statement = statement.where(InstructionRow.type == kind.value)
            statement = statement.order_by(InstructionRow.created_at, InstructionRow.id).limit(1)
            row = (await session.scalars(statement)).first()
            return None if row is None else to_instruction(row)

    async def get(self, instruction_id: str) -> Instruction | None:
        """Return the record by id, or None when the id is unknown."""
        async with self._session_factory() as session:
            row = await session.get(InstructionRow, instruction_id)
            return None if row is None else to_instruction(row)

    async def list_with_embeddings(self, user_id: str | None) -> list[EmbeddedInstruction]:
        """Return records visible to `user_id` (None = the whole table)."""
        async with self._session_factory() as session:
            statement = select(InstructionRow).order_by(InstructionRow.id)
            if user_id is not None:
                statement = statement.where(
                    InstructionRow.owner_id.is_(None) | (InstructionRow.owner_id == user_id)
                )
            rows = (await session.scalars(statement)).all()
        # off the event loop: mapping N rows of 1024-float embeddings into DTOs
        # is pure-Python CPU work that grows with the table
        return await asyncio.to_thread(
            lambda: [
                EmbeddedInstruction(
                    instruction=to_instruction(row),
                    embedding=tuple(row.embedding),
                )
                for row in rows
            ]
        )

    async def list_system(self) -> list[Instruction]:
        """Return every system (registry-owned) record, oldest first."""
        async with self._session_factory() as session:
            statement = (
                select(InstructionRow)
                .where(InstructionRow.system.is_(True))
                .order_by(InstructionRow.created_at, InstructionRow.id)
            )
            rows = (await session.scalars(statement)).all()
            return [to_instruction(row) for row in rows]

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        """Increment usage_count of the given records (search hits proved useful)."""
        if not instruction_ids:
            return
        async with self._session_factory() as session:
            await session.execute(
                update(InstructionRow)
                .where(InstructionRow.id.in_(instruction_ids))
                .values(usage_count=InstructionRow.usage_count + 1)
            )
            await session.commit()

    async def delete_by_id(self, instruction_id: str, owner_id: str) -> bool:
        """Delete the owner's private record by id; return True when removed."""
        async with self._session_factory() as session:
            statement = delete(InstructionRow).where(
                InstructionRow.id == instruction_id,
                InstructionRow.owner_id == owner_id,
            )
            # DML executes into a CursorResult at runtime; narrow for rowcount.
            result = cast(CursorResult[Any], await session.execute(statement))
            await session.commit()
            return result.rowcount > 0

    async def publish(self, instruction_id: str) -> Instruction | None:
        """Make the record public (owner_id -> NULL); None when the id is unknown.

        A memory-type record also answers None: memories are never published,
        and the admin surface treats an unpublishable record as a missing one.
        """
        async with self._session_factory() as session:
            row = await session.get(InstructionRow, instruction_id)
            if row is None or row.type == InstructionType.MEMORY.value:
                return None
            if row.author_id is None:
                # publication must not lose who wrote it: the departing
                # owner is the author (also backfills pre-authorship rows)
                row.author_id = row.owner_id
            row.owner_id = None
            row.updated_at = utc_now()
            await session.commit()
            return to_instruction(row)

    async def list_stale_embeddings(self, model: str, limit: int) -> list[Instruction]:
        """Return up to `limit` records whose vector `model` did not produce.

        Three cases collapse into one query. An empty embedding is a save whose
        backend was down or a row written by a migration; a NULL model is a row
        that predates the column, so nothing knows what wrote it; a different
        model means `OF_EMBEDDING_MODEL` changed and the stored vector is not
        comparable with anything the live model produces.
        """
        if limit <= 0:
            return []
        async with self._session_factory() as session:
            statement = (
                select(InstructionRow)
                .where(
                    (func.json_array_length(InstructionRow.embedding) == 0)
                    | InstructionRow.embedding_model.is_(None)
                    | (InstructionRow.embedding_model != model)
                )
                .order_by(InstructionRow.created_at, InstructionRow.id)
                .limit(limit)
            )
            rows = (await session.scalars(statement)).all()
            return [to_instruction(row) for row in rows]

    async def count_stale_embeddings(self, model: str) -> int:
        """How many records still need `model` to embed them (for the startup log)."""
        async with self._session_factory() as session:
            statement = select(func.count()).where(
                (func.json_array_length(InstructionRow.embedding) == 0)
                | InstructionRow.embedding_model.is_(None)
                | (InstructionRow.embedding_model != model)
            )
            return await session.scalar(statement) or 0

    async def set_embedding(
        self,
        instruction_id: str,
        embedding: tuple[float, ...],
        model: str,
    ) -> bool:
        """Store the embedding and its model; no version bump, no updated_at touch."""
        vector = list(embedding)
        async with self._session_factory() as session:
            statement = (
                update(InstructionRow)
                .where(InstructionRow.id == instruction_id)
                # re-embedding is maintenance, not an edit: the self-assignment
                # suppresses the column's onupdate stamp
                .values(
                    embedding=vector,
                    embedding_vector=vector or None,
                    embedding_model=model,
                    updated_at=InstructionRow.updated_at,
                )
            )
            result = cast(CursorResult[Any], await session.execute(statement))
            await session.commit()
            return result.rowcount > 0

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        """Delete the public/system record (kind, title); registry sync only."""
        async with self._session_factory() as session:
            statement = delete(InstructionRow).where(
                InstructionRow.type == kind.value,
                InstructionRow.title == title,
                InstructionRow.owner_id.is_(None),
            )
            # DML executes into a CursorResult at runtime; narrow for rowcount.
            result = cast(CursorResult[Any], await session.execute(statement))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    async def _find_row(
        session: AsyncSession,
        kind: InstructionType,
        title: str,
        owner_id: str | None,
    ) -> InstructionRow | None:
        result = await session.scalars(
            select(InstructionRow).where(
                InstructionRow.type == kind.value,
                InstructionRow.title == title,
                _owner_clause(owner_id),
            )
        )
        return result.first()


def _owner_clause(owner_id: str | None) -> ColumnElement[bool]:
    """Match the owner column exactly, where None means the public record."""
    if owner_id is None:
        return InstructionRow.owner_id.is_(None)
    return InstructionRow.owner_id == owner_id


def to_instruction(row: InstructionRow) -> Instruction:
    """Map an ORM row to the facade DTO (shared with `pg_store.py`)."""
    return Instruction(
        id=row.id,
        type=InstructionType(row.type),
        title=row.title,
        content=row.content,
        tags=tuple(row.tags),
        version=row.version,
        usage_count=row.usage_count,
        success_count=row.success_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        system=row.system,
        owner_id=row.owner_id,
        author_id=row.author_id,
    )
