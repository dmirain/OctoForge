"""Instruction store that lets Postgres do the searching.

Same table and same writes as `SqlAlchemyInstructionStore` — this subclass adds
the two capabilities the service detects by `isinstance`:

- `InstructionVectorSearch` (pgvector), so the service stops pulling every
  visible row into the process to rank it. At 10k records the brute-force path
  moves ~40 MB of floats across the driver and into Python objects on every
  recall, and recall runs on nearly every user message.
- `InstructionLexicalSearch` (pg_textsearch), the half embeddings are bad at:
  the exact product name, error code or field name where being *about* the same
  topic is not good enough.

Both are kept on a separate class rather than behind a flag because a store
that always carried the methods would claim capabilities the database may not
have, and the query would fail at the first recall instead of at startup. The
composition root probes `pg_extension` and picks what to build, which is what
makes a plain Postgres — or SQLite, or managed Postgres — a supported
configuration rather than a crash.
"""

import asyncio

import sqlalchemy as sa
from sqlalchemy import Select, select

from octoforge_core.instructions.api import EmbeddedInstruction, InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.ranking import fuse_rankings
from octoforge_core.instructions.store import SqlAlchemyInstructionStore, to_instruction

# Which BM25 index answers a text query. pg_textsearch picks the index from the
# column in the expression, so the name only has to exist; see migration
# c7e2a91f4d38 for how they are built and why the text search configuration is
# schema-qualified.
CONTENT_BM25_INDEX = "ix_instructions_bm25_content"
TITLE_BM25_INDEX = "ix_instructions_bm25_title"
# `<@>` scores a document sharing no term with the query as exactly 0, and every
# real match below it, so this is the boundary between "matched" and "did not".
NO_MATCH_SCORE = 0


class PostgresInstructionStore(SqlAlchemyInstructionStore):
    """SqlAlchemyInstructionStore plus pgvector and BM25 search."""

    async def search_by_vector(
        self,
        query_embedding: tuple[float, ...],
        limit: int,
        user_id: str | None,
        kinds: tuple[InstructionType, ...] = (),
    ) -> list[EmbeddedInstruction]:
        """Return up to `limit` visible records closest to the query, best first.

        Ordering is cosine distance (`<=>`), matching the cosine similarity the
        service scores with, so this shortlist is the set the brute-force path
        would have produced.

        Two filters are not optional. `embedding_vector IS NOT NULL` skips
        records whose embedding failed or has not been computed yet. The
        `vector_dims` check skips vectors of a different size: comparing them
        raises "different vector dimensions" outright, and the table holds two
        sizes at once while a changed embedding model is being absorbed by the
        startup sweep. Those records are not lost — they are re-embedded and
        come back — and stay reachable by exact title until then.
        """
        dimensions = len(query_embedding)
        if limit <= 0 or dimensions == 0:
            return []
        statement = (
            self._visible(select(InstructionRow), user_id, kinds)
            .where(
                InstructionRow.embedding_vector.is_not(None),
                sa.func.vector_dims(InstructionRow.embedding_vector) == dimensions,
            )
            .order_by(InstructionRow.embedding_vector.cosine_distance(list(query_embedding)))
            .limit(limit)
        )
        return await self._fetch(statement)

    async def search_by_text(
        self,
        query: str,
        limit: int,
        user_id: str | None,
        kinds: tuple[InstructionType, ...] = (),
    ) -> list[EmbeddedInstruction]:
        """Return up to `limit` visible records matching the query, best first.

        Two BM25 indexes are consulted, title and content, and their results
        are unioned. They are separate because BM25 normalizes by document
        length: a title averages a couple of tokens and a skill's body over a
        hundred, so folding them into one document would let the body drown a
        title match. Kept apart, a title hit and a body hit are two independent
        signals for the fusion to weigh.

        `<@>` is pg_textsearch's ranking operator; scores are negative, better
        matches lower, and a record sharing no term with the query is not
        returned at all.
        """
        if limit <= 0 or not query.strip():
            return []
        rankings = [
            await self._fetch(self._visible(statement, user_id, kinds).limit(limit))
            for statement in (
                self._ranked_by(InstructionRow.content, CONTENT_BM25_INDEX, query),
                self._ranked_by(InstructionRow.title, TITLE_BM25_INDEX, query),
            )
        ]
        # Two lexical signals merged into the one ordering this capability
        # promises. Combining them here rather than upstream keeps "how BM25 is
        # indexed" an implementation detail: the service fuses retrievers, not
        # indexes.
        return fuse_rankings(rankings)[:limit]

    @staticmethod
    def _ranked_by(
        column: sa.orm.Mapped[str],
        index_name: str,
        query: str,
    ) -> Select[tuple[InstructionRow]]:
        """Order by BM25 relevance against one indexed column, matches only.

        pg_textsearch exposes exactly one operator, `<@>`, which scores rather
        than filters: better matches score lower (they are negative) and a row
        sharing no term with the query scores exactly 0. So `< 0` is the match
        predicate.

        It has to be written out. The index's own top-k scan drops non-matches
        as a side effect, but adding the visibility and kind predicates can push
        the planner onto a sequential scan, which cheerfully scores every row in
        the table — and a non-match must be absent rather than ranked last, or
        it would still cast a reciprocal-rank vote in the fusion.

        The index is named explicitly because with two BM25 indexes on this
        table pg_textsearch cannot infer which one an expression means.
        """
        relevance = column.op("<@>")(sa.func.to_bm25query(query, index_name))
        return select(InstructionRow).where(relevance < NO_MATCH_SCORE).order_by(relevance)

    @staticmethod
    def _visible(
        statement: Select[tuple[InstructionRow]],
        user_id: str | None,
        kinds: tuple[InstructionType, ...],
    ) -> Select[tuple[InstructionRow]]:
        """Apply the visibility and type predicates inside the query.

        Both must be here rather than in the caller: `limit` is spent before
        any filtering the caller could do, so a top-20 that happens to be all
        skills would leave an endpoint search with nothing to show.
        """
        if user_id is not None:
            statement = statement.where(
                InstructionRow.owner_id.is_(None) | (InstructionRow.owner_id == user_id)
            )
        if kinds:
            statement = statement.where(InstructionRow.type.in_([kind.value for kind in kinds]))
        return statement

    async def _fetch(self, statement: Select[tuple[InstructionRow]]) -> list[EmbeddedInstruction]:
        """Run the query and map the rows to DTOs off the event loop."""
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
        # a shortlist rather than the whole table, but mapping still allocates
        # one tuple of floats per row: off the loop, as brute force already is
        return await asyncio.to_thread(
            lambda: [
                EmbeddedInstruction(
                    instruction=to_instruction(row),
                    embedding=tuple(row.embedding),
                )
                for row in rows
            ]
        )
