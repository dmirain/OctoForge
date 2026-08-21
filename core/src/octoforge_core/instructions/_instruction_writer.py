"""Race-safe instruction upserts and version updates."""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.instructions._instruction_rows import owner_clause, to_instruction
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.types import Instruction, InstructionDraft
from octoforge_core.time import utc_now

FIRST_VERSION = 1


class InstructionWriter:
    """Resolve concurrent inserts into one versioned instruction record."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def upsert(self, draft: InstructionDraft) -> Instruction:
        async with self._session_factory() as session:
            row = await self._find_draft(session, draft)
            if row is not None:
                return await self._update(session, row, draft)
            try:
                return await self._insert(session, draft)
            except IntegrityError:
                await session.rollback()
                winner = await self._find_draft(session, draft)
                if winner is None:
                    raise
                return await self._update(session, winner, draft)

    @staticmethod
    async def _insert(session: AsyncSession, draft: InstructionDraft) -> Instruction:
        vector = list(draft.embedding)
        row = InstructionRow(
            id=uuid.uuid4().hex,
            type=draft.kind.value,
            title=draft.title,
            content=draft.content,
            embedding=vector,
            embedding_vector=vector or None,
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

    @staticmethod
    async def _update(
        session: AsyncSession,
        row: InstructionRow,
        draft: InstructionDraft,
    ) -> Instruction:
        vector = list(draft.embedding)
        row.content = draft.content
        row.embedding = vector
        row.embedding_vector = vector or None
        row.embedding_model = draft.embedding_model
        row.tags = list(draft.tags)
        row.version += 1
        row.system = draft.system
        if draft.author_id is not None:
            row.author_id = draft.author_id
        row.updated_at = utc_now()
        await session.commit()
        return to_instruction(row)

    @staticmethod
    async def _find_draft(session: AsyncSession, draft: InstructionDraft) -> InstructionRow | None:
        rows = await session.scalars(
            select(InstructionRow).where(
                InstructionRow.type == draft.kind.value,
                InstructionRow.title == draft.title,
                owner_clause(draft.owner_id),
            )
        )
        return rows.first()
