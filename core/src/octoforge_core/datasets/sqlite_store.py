"""Dataset store that adds FTS5 search over descriptions on SQLite.

The embedded counterpart of `pg_store.PostgresDatasetStore`, with the same
modest ambition: an owner has a handful of descriptors, so this is not about
speed. It finds the abbreviation or internal code that sits in a description
but nowhere near the query in vector space.
"""

import sqlalchemy as sa
from sqlalchemy import select

from octoforge_core.datasets.api import EmbeddedDataset
from octoforge_core.datasets.models import DatasetRow
from octoforge_core.datasets.store import SqlAlchemyDatasetStore, to_embedded_dataset
from octoforge_core.db.sqlite_fts import FTS_TABLES, match_expression, rank_expression

DATASETS_FTS = FTS_TABLES[2]


class SqliteDatasetStore(SqlAlchemyDatasetStore):
    """SqlAlchemyDatasetStore plus BM25 search over the FTS5 mirror."""

    async def search_by_text(
        self,
        owner_user_id: str,
        query: str,
        limit: int,
    ) -> list[EmbeddedDataset]:
        """Return up to `limit` of this owner's descriptors matching the words."""
        expression = match_expression(query)
        if limit <= 0 or expression is None:
            return []
        statement = sa.text(
            f"SELECT d.id FROM {DATASETS_FTS.name} f "
            f"JOIN {DATASETS_FTS.source} d ON d.rowid = f.rowid "
            f"WHERE {DATASETS_FTS.name} MATCH :expression AND d.owner_user_id = :owner "
            f"ORDER BY {rank_expression(DATASETS_FTS)} LIMIT :limit"
        )
        async with self._session_factory() as session:
            ids = [
                str(row[0])
                for row in await session.execute(
                    statement,
                    {"expression": expression, "owner": owner_user_id, "limit": limit},
                )
            ]
            if not ids:
                return []
            rows = (await session.scalars(select(DatasetRow).where(DatasetRow.id.in_(ids)))).all()
        by_id = {row.id: row for row in rows}
        return [to_embedded_dataset(by_id[key]) for key in ids if key in by_id]
