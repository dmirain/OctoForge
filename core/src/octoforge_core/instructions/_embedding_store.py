"""Detection and maintenance of stale instruction embeddings."""

from typing import Any, cast

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.instructions._instruction_rows import to_instruction
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.types import Instruction


class InstructionEmbeddingStore:
    """Find vectors from another model and replace them without editing records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_stale(self, model: str, limit: int) -> list[Instruction]:
        if limit <= 0:
            return []
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(
                select(InstructionRow)
                .where(_stale_clause(model))
                .order_by(InstructionRow.created_at, InstructionRow.id)
                .limit(limit)
            )
            return [to_instruction(row) for row in rows.all()]

    async def count_stale(self, model: str) -> int:
        async with read_session(self._session_factory) as session:
            return await session.scalar(select(func.count()).where(_stale_clause(model))) or 0

    async def set(
        self,
        instruction_id: str,
        embedding: tuple[float, ...],
        model: str,
    ) -> bool:
        vector = list(embedding)
        async with write_session(self._session_factory) as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(InstructionRow)
                    .where(InstructionRow.id == instruction_id)
                    .values(
                        embedding=vector,
                        embedding_vector=vector or None,
                        embedding_model=model,
                        updated_at=InstructionRow.updated_at,
                    )
                ),
            )
            return result.rowcount > 0


def _stale_clause(model: str) -> ColumnElement[bool]:
    return (
        (func.json_array_length(InstructionRow.embedding) == 0)
        | InstructionRow.embedding_model.is_(None)
        | (InstructionRow.embedding_model != model)
    )
