"""Instruction store that adds FTS5 lexical search on SQLite.

The embedded counterpart of `pg_store.PostgresInstructionStore`, claiming only
the lexical capability: SQLite has no vector type, so ranking by embedding
stays in the process where it always was.

What the two dialects do NOT share is Russian morphology — see
`db/sqlite_fts.py`. Latin terms behave identically; Russian is matched by
substring rather than by stem, so this path finds "задачи" from "задач" but
not from "задача".
"""

import asyncio

import sqlalchemy as sa
from sqlalchemy import select

from octoforge_core.db.sqlite_fts import FTS_TABLES, match_expression, rank_expression
from octoforge_core.db.unit_of_work import read_session
from octoforge_core.instructions.api import EmbeddedInstruction, InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.instructions.store import SqlAlchemyInstructionStore, to_instruction

INSTRUCTIONS_FTS = FTS_TABLES[0]


class SqliteInstructionStore(SqlAlchemyInstructionStore):
    """SqlAlchemyInstructionStore plus BM25 search over the FTS5 mirror."""

    async def search_by_text(
        self,
        query: str,
        limit: int,
        user_id: str | None,
        kinds: tuple[InstructionType, ...] = (),
    ) -> list[EmbeddedInstruction]:
        """Return up to `limit` visible records matching the query, best first."""
        expression = match_expression(query)
        if limit <= 0 or expression is None:
            return []
        ids = await self._matching_ids(expression, limit, user_id, kinds)
        if not ids:
            return []
        async with read_session(self._session_factory) as session:
            rows = (
                await session.scalars(select(InstructionRow).where(InstructionRow.id.in_(ids)))
            ).all()
        by_id = {row.id: row for row in rows}
        # the FTS query decided the order; the second lookup only rehydrates
        ordered = [by_id[key] for key in ids if key in by_id]
        return await asyncio.to_thread(
            lambda: [
                EmbeddedInstruction(
                    instruction=to_instruction(row),
                    embedding=tuple(row.embedding),
                )
                for row in ordered
            ]
        )

    async def _matching_ids(
        self,
        expression: str,
        limit: int,
        user_id: str | None,
        kinds: tuple[InstructionType, ...],
    ) -> list[str]:
        """Ask the FTS5 mirror for the matching ids, best first.

        Raw SQL because `MATCH` and `bm25()` are FTS5 syntax with no ORM
        equivalent. The visibility and kind predicates live in this query
        rather than in the caller for the same reason as on Postgres: `limit`
        is spent before anything downstream could filter.
        """
        clauses = [f"{INSTRUCTIONS_FTS.name} MATCH :expression"]
        params: dict[str, object] = {"expression": expression, "limit": limit}
        if user_id is not None:
            clauses.append("(i.owner_id IS NULL OR i.owner_id = :user_id)")
            params["user_id"] = user_id
        if kinds:
            names = [kind.value for kind in kinds]
            placeholders = ", ".join(f":kind{index}" for index in range(len(names)))
            clauses.append(f"i.type IN ({placeholders})")
            params.update({f"kind{index}": name for index, name in enumerate(names)})
        statement = sa.text(
            f"SELECT i.id FROM {INSTRUCTIONS_FTS.name} f "
            f"JOIN {INSTRUCTIONS_FTS.source} i ON i.rowid = f.rowid "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY {rank_expression(INSTRUCTIONS_FTS)} LIMIT :limit"
        )
        async with read_session(self._session_factory) as session:
            rows = await session.execute(statement, params)
            return [str(row[0]) for row in rows]
