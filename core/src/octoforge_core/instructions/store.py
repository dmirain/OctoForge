"""SQLAlchemy storage of the instructions module (module-internal).

Follows the `db/repositories.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to facade DTOs at the boundary.
"""

import uuid
from typing import Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.instructions.api import Instruction, InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.ranking import EmbeddedInstruction
from octoforge_core.time import utc_now

FIRST_VERSION = 1


class InstructionStore:
    """SQL persistence for instruction records and their embeddings."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...],
        embedding: tuple[float, ...],
    ) -> Instruction:
        """Create the record or replace content/tags/embedding, bumping the version."""
        async with self._session_factory() as session:
            row = await self._find_row(session, kind, title)
            if row is None:
                row = InstructionRow(
                    id=uuid.uuid4().hex,
                    type=kind.value,
                    title=title,
                    content=content,
                    embedding=list(embedding),
                    tags=list(tags),
                    version=FIRST_VERSION,
                )
                session.add(row)
            else:
                row.content = content
                row.embedding = list(embedding)
                row.tags = list(tags)
                row.version += 1
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
            rows = (await session.scalars(select(InstructionRow))).all()
            return [
                EmbeddedInstruction(
                    instruction=_to_instruction(row),
                    embedding=tuple(row.embedding),
                )
                for row in rows
            ]

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
    )
