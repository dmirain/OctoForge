"""Instruction store with Postgres-side vector and lexical ranking."""

import sqlalchemy as sa
from sqlalchemy import select

from octoforge_core.instructions._postgres_candidates import fetch_candidates, visible
from octoforge_core.instructions._postgres_lexical import search_text
from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    InstructionTextQuery,
    InstructionVectorQuery,
)
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.store import SqlAlchemyInstructionStore


class PostgresInstructionStore(SqlAlchemyInstructionStore):
    """Portable instruction persistence plus pgvector and BM25 capabilities."""

    async def search_by_vector(
        self,
        request: InstructionVectorQuery,
    ) -> list[EmbeddedInstruction]:
        """Return visible records closest by cosine distance."""
        dimensions = len(request.embedding)
        if request.limit <= 0 or dimensions == 0:
            return []
        statement = (
            visible(select(InstructionRow), request.user_id, request.kinds)
            .where(
                InstructionRow.embedding_vector.is_not(None),
                sa.func.vector_dims(InstructionRow.embedding_vector) == dimensions,
            )
            .order_by(InstructionRow.embedding_vector.cosine_distance(list(request.embedding)))
            .limit(request.limit)
        )
        return await fetch_candidates(self._session_factory, statement)

    async def search_by_text(
        self,
        request: InstructionTextQuery,
    ) -> list[EmbeddedInstruction]:
        return await search_text(self._session_factory, request)
