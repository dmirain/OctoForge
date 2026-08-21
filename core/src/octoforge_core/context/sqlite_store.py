"""Summary store whose archive search uses FTS5 instead of a substring match.

The embedded counterpart of `pg_store.PostgresSummaryStore`. Same contract as
the portable implementation — dialog scope, seq and date filters, the limit —
with results ranked by BM25 rather than returned in seq order.

The Russian caveat from `db/sqlite_fts.py` applies: matching is by substring,
not by stem, so "задач" finds "задачи" but "задача" does not.
"""

import sqlalchemy as sa
from sqlalchemy import select

from octoforge_core.context.api import ArchivedMessage, ArchiveFilter, ArchiveSearch
from octoforge_core.context.store import SqlAlchemySummaryStore, to_archived
from octoforge_core.db.sqlite_fts import FTS_TABLES, match_expression, rank_expression
from octoforge_core.dialogs.models import MessageRow

MESSAGES_FTS = FTS_TABLES[1]


class SqliteSummaryStore(SqlAlchemySummaryStore):
    """SqlAlchemySummaryStore whose `search` ranks by BM25 over the FTS5 mirror."""

    async def search(self, request: ArchiveSearch) -> list[ArchivedMessage]:
        """Return the messages most relevant to the query, best first."""
        expression = match_expression(request.query)
        restriction = request.filters if request.filters is not None else ArchiveFilter()
        if expression is None or restriction.seq_ranges == () or request.limit <= 0:
            return []
        seqs = await self._matching_seqs(request, expression)
        if not seqs:
            return []
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(MessageRow).where(
                        MessageRow.dialog_id == request.dialog_id,
                        MessageRow.seq.in_(seqs),
                    )
                )
            ).all()
        by_seq = {row.seq: row for row in rows}
        return [to_archived(by_seq[seq]) for seq in seqs if seq in by_seq]

    async def _matching_seqs(
        self,
        request: ArchiveSearch,
        expression: str,
    ) -> list[int]:
        """Ask the FTS5 mirror for the matching seqs of this dialog, best first."""
        restriction = request.filters if request.filters is not None else ArchiveFilter()
        clauses = [f"{MESSAGES_FTS.name} MATCH :expression", "m.dialog_id = :dialog_id"]
        params: dict[str, object] = {
            "expression": expression,
            "dialog_id": request.dialog_id,
            "limit": request.limit,
        }
        if restriction.seq_ranges is not None:
            ranges = [
                f"(m.seq >= :from{index} AND m.seq <= :to{index})"
                for index in range(len(restriction.seq_ranges))
            ]
            clauses.append(f"({' OR '.join(ranges)})")
            for index, (seq_from, seq_to) in enumerate(restriction.seq_ranges):
                params[f"from{index}"] = seq_from
                params[f"to{index}"] = seq_to
        if restriction.date_from is not None:
            clauses.append("m.created_at >= :date_from")
            params["date_from"] = restriction.date_from
        if restriction.date_to is not None:
            clauses.append("m.created_at < :date_to")
            params["date_to"] = restriction.date_to
        statement = sa.text(
            f"SELECT m.seq FROM {MESSAGES_FTS.name} f "
            f"JOIN {MESSAGES_FTS.source} m ON m.rowid = f.rowid "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY {rank_expression(MESSAGES_FTS)} LIMIT :limit"
        )
        async with self._session_factory() as session:
            rows = await session.execute(statement, params)
            return [int(row[0]) for row in rows]
