"""Instruction read projections, visibility, and stable ordering."""

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session
from octoforge_core.instructions._instruction_rows import owner_clause, to_instruction
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.types import (
    EmbeddedInstruction,
    Instruction,
    InstructionType,
)


class InstructionQueries:
    """Read records with owner visibility and map ORM rows off the event loop."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_title(
        self,
        title: str,
        kind: InstructionType | None,
        owner_id: str | None,
    ) -> Instruction | None:
        async with read_session(self._session_factory) as session:
            statement = select(InstructionRow).where(
                InstructionRow.title == title,
                owner_clause(owner_id),
            )
            if kind is not None:
                statement = statement.where(InstructionRow.type == kind.value)
            statement = statement.order_by(InstructionRow.created_at, InstructionRow.id).limit(1)
            row = (await session.scalars(statement)).first()
            return None if row is None else to_instruction(row)

    async def get(self, instruction_id: str) -> Instruction | None:
        async with read_session(self._session_factory) as session:
            row = await session.get(InstructionRow, instruction_id)
            return None if row is None else to_instruction(row)

    async def list_with_embeddings(self, user_id: str | None) -> list[EmbeddedInstruction]:
        async with read_session(self._session_factory) as session:
            statement = select(InstructionRow).order_by(InstructionRow.id)
            if user_id is not None:
                statement = statement.where(
                    InstructionRow.owner_id.is_(None) | (InstructionRow.owner_id == user_id)
                )
            rows = (await session.scalars(statement)).all()
        return await asyncio.to_thread(
            lambda: [EmbeddedInstruction(to_instruction(row), tuple(row.embedding)) for row in rows]
        )

    async def list_system(self) -> list[Instruction]:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(InstructionRow)
                .where(InstructionRow.system.is_(True))
                .order_by(InstructionRow.created_at, InstructionRow.id)
            )
            return [to_instruction(row) for row in rows.all()]

    async def list_public_by_prefix(self, kind: InstructionType, prefix: str) -> list[Instruction]:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(InstructionRow)
                .where(
                    InstructionRow.type == kind.value,
                    InstructionRow.owner_id.is_(None),
                    InstructionRow.system.is_(False),
                    InstructionRow.title.startswith(prefix, autoescape=True),
                )
                .order_by(InstructionRow.title)
            )
            return [to_instruction(row) for row in rows.all()]

    async def memory_chars(self, owner_id: str) -> int:
        async with read_session(self._session_factory) as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(func.length(InstructionRow.content)), 0)).where(
                    InstructionRow.type == InstructionType.MEMORY.value,
                    InstructionRow.owner_id == owner_id,
                )
            )
            return int(total or 0)
