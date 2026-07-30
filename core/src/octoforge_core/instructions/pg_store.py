"""Instruction store that lets Postgres do the vector search.

Same table and same writes as `SqlAlchemyInstructionStore` — this subclass adds
one thing: the `InstructionVectorSearch` capability, so the service stops
pulling every visible row into the process to rank it. At 10k records the
brute-force path moves ~40 MB of floats across the driver and into Python
objects on every recall, and recall runs on nearly every user message.

Kept as a separate class rather than a flag on the portable store because the
capability is detected with `isinstance`: a store that always had the method
would claim vector search on a database without pgvector, and the query would
fail at the first search instead of at startup. The composition root probes
`pg_extension` and picks the class, which is also what makes "no pgvector" a
supported configuration rather than a crash.
"""

import asyncio

import sqlalchemy as sa
from sqlalchemy import select

from octoforge_core.instructions.api import EmbeddedInstruction
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.store import SqlAlchemyInstructionStore, to_instruction


class PostgresInstructionStore(SqlAlchemyInstructionStore):
    """SqlAlchemyInstructionStore plus pgvector nearest-neighbour search."""

    async def search_by_vector(
        self,
        query_embedding: tuple[float, ...],
        limit: int,
        user_id: str | None,
    ) -> list[EmbeddedInstruction]:
        """Return up to `limit` visible records closest to the query, best first.

        Ordering is cosine distance (`<=>`), which matches the cosine similarity
        the service scores with, so the shortlist this returns is the same set
        the brute-force path would have produced.

        Two filters are not optional. `embedding_vector IS NOT NULL` skips
        records whose embedding failed or has not been computed yet. The
        `vector_dims` check skips vectors of a different size: comparing them
        raises "different vector dimensions" outright, and a table can hold two
        sizes at once while a changed embedding model is being absorbed by the
        startup sweep. Those records are not lost — they are re-embedded and
        come back — and until then they stay reachable by exact title.
        """
        dimensions = len(query_embedding)
        if limit <= 0 or dimensions == 0:
            return []
        vector = list(query_embedding)
        statement = (
            select(InstructionRow)
            .where(
                InstructionRow.embedding_vector.is_not(None),
                sa.func.vector_dims(InstructionRow.embedding_vector) == dimensions,
            )
            .order_by(InstructionRow.embedding_vector.cosine_distance(vector))
            .limit(limit)
        )
        if user_id is not None:
            statement = statement.where(
                InstructionRow.owner_id.is_(None) | (InstructionRow.owner_id == user_id)
            )
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        # `limit` rows rather than the whole table, but mapping them still
        # allocates one tuple of floats each: off the loop, as the brute-force
        # path already does
        return await asyncio.to_thread(
            lambda: [
                EmbeddedInstruction(
                    instruction=to_instruction(row),
                    embedding=tuple(row.embedding),
                )
                for row in rows
            ]
        )
