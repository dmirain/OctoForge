"""Dataset store that adds BM25 ranking over descriptor descriptions.

Same writes as `SqlAlchemyDatasetStore`; the addition is the
`DatasetLexicalSearch` capability the service detects by isinstance.

Worth being plain about the size of the win. An owner has a handful of dataset
descriptors, so nothing here is about speed — brute-force cosine over ten rows
was never a problem. It is about the matches embeddings do not make: an
abbreviation, an internal code, a proper noun that sits in the description but
nowhere near the query in vector space.
"""

import sqlalchemy as sa
from sqlalchemy import select

from octoforge_core.datasets.api import EmbeddedDataset
from octoforge_core.datasets.models import DatasetRow
from octoforge_core.datasets.store import SqlAlchemyDatasetStore, to_embedded_dataset
from octoforge_core.db.unit_of_work import read_session

DESCRIPTION_BM25_INDEX = "ix_datasets_bm25_description"
# `<@>` scores a document sharing no term with the query as exactly 0 and every
# real match below it, so this is the boundary between matched and not.
NO_MATCH_SCORE = 0


class PostgresDatasetStore(SqlAlchemyDatasetStore):
    """SqlAlchemyDatasetStore plus BM25 search over descriptions."""

    async def search_by_text(
        self,
        owner_user_id: str,
        query: str,
        limit: int,
    ) -> list[EmbeddedDataset]:
        """Return up to `limit` of this owner's descriptors matching the words."""
        if limit <= 0 or not query.strip():
            return []
        relevance = DatasetRow.description.op("<@>")(
            sa.func.to_bm25query(query, DESCRIPTION_BM25_INDEX)
        )
        statement = (
            select(DatasetRow)
            .where(DatasetRow.owner_user_id == owner_user_id, relevance < NO_MATCH_SCORE)
            .order_by(relevance)
            .limit(limit)
        )
        async with read_session(self._session_factory) as session:
            rows = (await session.scalars(statement)).all()
            return [to_embedded_dataset(row) for row in rows]
