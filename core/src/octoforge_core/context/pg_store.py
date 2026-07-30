"""Summary store whose archive search is ranked by BM25 instead of substring.

`SqlAlchemySummaryStore.search` matches `content ILIKE '%query%'` and returns
the first `limit` hits in seq order. That is the portable behaviour and it stays
the fallback, but it has two problems this subclass exists to fix.

It does not stem. In Russian that is close to fatal: "задача" does not find
"задачи", so the tool only works when the user types the exact inflected form
somebody wrote months earlier. And it does not rank — the first `limit`
substring hits win on position in the dialog, not on how well they answer the
question, over a table that by design never deletes a row.

Same class-not-flag reasoning as the instruction stores: the composition root
probes for pg_textsearch and builds this only when the index can exist.
"""

import sqlalchemy as sa
from sqlalchemy import and_, or_, select

from octoforge_core.context.api import ArchivedMessage, ArchiveFilter
from octoforge_core.context.store import SqlAlchemySummaryStore, to_archived
from octoforge_core.dialogs.models import MessageRow

MESSAGE_BM25_INDEX = "ix_messages_bm25_content"
# `<@>` scores a document sharing no term with the query as exactly 0 and every
# real match below it, so this is the boundary between matched and not.
NO_MATCH_SCORE = 0


class PostgresSummaryStore(SqlAlchemySummaryStore):
    """SqlAlchemySummaryStore whose `search` ranks by BM25 relevance."""

    async def search(
        self,
        dialog_id: str,
        query: str,
        *,
        filters: ArchiveFilter | None = None,
        limit: int,
    ) -> list[ArchivedMessage]:
        """Return the messages most relevant to the query, best first.

        Same contract as the portable implementation — dialog scope, the seq
        and date filters, the limit — with two differences the caller feels as
        better answers: the query is stemmed, so an inflected form matches, and
        results come back by relevance rather than by position in the dialog.
        """
        needle = query.strip()
        restriction = filters if filters is not None else ArchiveFilter()
        if not needle or restriction.seq_ranges == ():
            return []
        relevance = MessageRow.content.op("<@>")(sa.func.to_bm25query(needle, MESSAGE_BM25_INDEX))
        clauses = [MessageRow.dialog_id == dialog_id, relevance < NO_MATCH_SCORE]
        if restriction.seq_ranges is not None:
            clauses.append(
                or_(
                    *(
                        and_(MessageRow.seq >= seq_from, MessageRow.seq <= seq_to)
                        for seq_from, seq_to in restriction.seq_ranges
                    )
                )
            )
        if restriction.date_from is not None:
            clauses.append(MessageRow.created_at >= restriction.date_from)
        if restriction.date_to is not None:
            clauses.append(MessageRow.created_at < restriction.date_to)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(MessageRow).where(*clauses).order_by(relevance).limit(limit)
            )
            return [to_archived(row) for row in rows.all()]
