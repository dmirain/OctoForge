"""Counters, owner-scoped deletion, and publication mutations."""

from typing import Any, cast

from sqlalchemy import delete, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import write_session
from octoforge_core.instructions._instruction_rows import to_instruction
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.types import Instruction, InstructionType
from octoforge_core.time import utc_now


class InstructionCommands:
    """Apply mutations that do not change instruction content or embeddings."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def bump_usage(self, instruction_ids: tuple[str, ...]) -> None:
        if not instruction_ids:
            return
        async with write_session(self._session_factory) as session:
            await session.execute(
                update(InstructionRow)
                .where(InstructionRow.id.in_(instruction_ids))
                .values(usage_count=InstructionRow.usage_count + 1)
            )

    async def delete_by_id(self, instruction_id: str, owner_id: str) -> bool:
        async with write_session(self._session_factory) as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(InstructionRow).where(
                        InstructionRow.id == instruction_id,
                        InstructionRow.owner_id == owner_id,
                    )
                ),
            )
            return result.rowcount > 0

    async def publish(self, instruction_id: str) -> Instruction | None:
        async with write_session(self._session_factory) as session:
            row = await session.get(InstructionRow, instruction_id)
            if row is None or row.type == InstructionType.MEMORY.value:
                return None
            if row.author_id is None:
                row.author_id = row.owner_id
            row.owner_id = None
            row.updated_at = utc_now()
            return to_instruction(row)

    async def delete_by_title(self, title: str, kind: InstructionType) -> bool:
        async with write_session(self._session_factory) as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(InstructionRow).where(
                        InstructionRow.type == kind.value,
                        InstructionRow.title == title,
                        InstructionRow.owner_id.is_(None),
                    )
                ),
            )
            return result.rowcount > 0
