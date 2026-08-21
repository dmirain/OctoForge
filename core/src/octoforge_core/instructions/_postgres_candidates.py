"""Visibility predicates and row mapping shared by Postgres retrievers."""

import asyncio

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session
from octoforge_core.instructions._instruction_rows import to_instruction
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.types import EmbeddedInstruction, InstructionType


def visible(
    statement: Select[tuple[InstructionRow]],
    user_id: str | None,
    kinds: tuple[InstructionType, ...],
) -> Select[tuple[InstructionRow]]:
    """Apply filters before the candidate limit spends its budget."""
    if user_id is not None:
        statement = statement.where(
            InstructionRow.owner_id.is_(None) | (InstructionRow.owner_id == user_id)
        )
    if kinds:
        statement = statement.where(InstructionRow.type.in_([kind.value for kind in kinds]))
    return statement


async def fetch_candidates(
    session_factory: async_sessionmaker[AsyncSession],
    statement: Select[tuple[InstructionRow]],
) -> list[EmbeddedInstruction]:
    async with read_session(session_factory) as session:
        rows = (await session.scalars(statement)).all()
    return await asyncio.to_thread(
        lambda: [EmbeddedInstruction(to_instruction(row), tuple(row.embedding)) for row in rows]
    )
