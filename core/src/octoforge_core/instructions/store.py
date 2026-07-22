"""SQLAlchemy storage of the instructions module.

Follows the `db/repositories.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to facade DTOs at the boundary.
Implements the `InstructionStore` port from `instructions/api.py`; the
vector-search capability (`InstructionVectorSearch`) is deliberately not
implemented — this store ranks brute-force in the process.
"""

import uuid
from typing import Any, cast

from sqlalchemy import delete, select, update
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
            row = await self._find_row(session, draft.kind, draft.title)
            if row is not None:
                return await self._update_row(session, row, draft)
            try:
                return await self._insert_row(session, draft)
            except IntegrityError:
                # lost the find-then-insert race with a concurrent process
                # (e.g. web + standalone syncing the registry at startup over
                # one SQLite file): redo as an update of the winner's row
                await session.rollback()
                winner = await self._find_row(session, draft.kind, draft.title)
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
            tags=list(draft.tags),
            version=FIRST_VERSION,
            system=draft.system,
        )
        session.add(row)
        await session.commit()
        return _to_instruction(row)

    async def _update_row(
        self,
        session: AsyncSession,
        row: InstructionRow,
        draft: InstructionDraft,
    ) -> Instruction:
        row.content = draft.content
        row.embedding = list(draft.embedding)
        row.tags = list(draft.tags)
        row.version += 1
        row.system = draft.system
        row.updated_at = utc_now()
        await session.commit()
        return _to_instruction(row)

    async def get_by_title(self, title: str, kind: InstructionType | None) -> Instruction | None:
        """Return the record by title (oldest first when types collide), or None."""
        async with self._session_factory() as session:
            statement = select(InstructionRow).where(InstructionRow.title == title)
            if kind is not None:
                statement = statement.where(InstructionRow.type == kind.value)
            statement = statement.order_by(InstructionRow.created_at, InstructionRow.id).limit(1)
            row = (await session.scalars(statement)).first()
            return None if row is None else _to_instruction(row)

    async def list_with_embeddings(self) -> list[EmbeddedInstruction]:
        """Return every record with its embedding (brute-force search input)."""
        async with self._session_factory() as session:
            rows = (await session.scalars(select(InstructionRow).order_by(InstructionRow.id))).all()
            return [
                EmbeddedInstruction(
                    instruction=_to_instruction(row),
                    embedding=tuple(row.embedding),
                )
                for row in rows
            ]

    async def list_system(self) -> list[Instruction]:
        """Return every system (registry-owned) record, oldest first."""
        async with self._session_factory() as session:
            statement = (
                select(InstructionRow)
                .where(InstructionRow.system.is_(True))
                .order_by(InstructionRow.created_at, InstructionRow.id)
            )
            rows = (await session.scalars(statement)).all()
            return [_to_instruction(row) for row in rows]

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

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        """Delete the record identified by (kind, title); return True when removed."""
        async with self._session_factory() as session:
            statement = delete(InstructionRow).where(
                InstructionRow.type == kind.value,
                InstructionRow.title == title,
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
    ) -> InstructionRow | None:
        result = await session.scalars(
            select(InstructionRow).where(
                InstructionRow.type == kind.value,
                InstructionRow.title == title,
            )
        )
        return result.first()


def _to_instruction(row: InstructionRow) -> Instruction:
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
    )
