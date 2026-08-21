"""SQLAlchemy storage of the datasets module.

Follows the `instructions/store.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to facade DTOs at the boundary.
Implements the `DatasetStore` port from `datasets/api.py`; the vector-search
capability (`DatasetVectorSearch`) is deliberately not implemented — this
store ranks brute-force in the process.
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.datasets._commands import create_dataset
from octoforge_core.datasets._queries import find_dataset_row, query_records
from octoforge_core.datasets._rows import to_dataset, to_embedded_dataset, to_record
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.datasets.requests import DatasetDefinition, DatasetRecordScan
from octoforge_core.datasets.types import Dataset, DatasetRecord, EmbeddedDataset
from octoforge_core.db.unit_of_work import read_session, write_session


class SqlAlchemyDatasetStore:
    """SQL persistence for dataset descriptors and their records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        definition: DatasetDefinition,
        embedding: tuple[float, ...],
    ) -> Dataset:
        """Insert the descriptor; the owner/name constraint guards races."""
        return await create_dataset(self._session_factory, definition, embedding)

    async def get(self, owner_user_id: str, name: str) -> Dataset | None:
        """Return the descriptor of this owner by name, or None."""
        async with read_session(self._session_factory) as session:
            row = await find_dataset_row(session, owner_user_id, name)
            return None if row is None else to_dataset(row)

    async def add_record(
        self,
        dataset_id: str,
        owner_user_id: str,
        payload: dict[str, Any],
    ) -> DatasetRecord:
        """Append a record row to the dataset."""
        async with write_session(self._session_factory) as session:
            row = DatasetRecordRow(
                id=uuid.uuid4().hex,
                dataset_id=dataset_id,
                owner_user_id=owner_user_id,
                payload=payload,
            )
            session.add(row)
            await session.flush()
            return to_record(row)

    async def delete(self, owner_user_id: str, name: str) -> int | None:
        """Delete the descriptor and explicitly cascade to its records.

        The cascade is an explicit DELETE because SQLite runs without
        PRAGMA foreign_keys, so the FK alone would not remove the records.
        Returns the number of deleted records, or None when no such dataset.
        """
        async with write_session(self._session_factory) as session:
            row = await find_dataset_row(session, owner_user_id, name)
            if row is None:
                return None
            records_count = (
                await session.scalars(
                    select(func.count())
                    .select_from(DatasetRecordRow)
                    .where(DatasetRecordRow.dataset_id == row.id)
                )
            ).one()
            await session.execute(
                delete(DatasetRecordRow).where(DatasetRecordRow.dataset_id == row.id)
            )
            await session.delete(row)
            return records_count

    async def list_with_embeddings(self, owner_user_id: str) -> list[EmbeddedDataset]:
        """Return every descriptor of this owner with its embedding (search input)."""
        async with read_session(self._session_factory) as session:
            rows = (
                await session.scalars(
                    select(DatasetRow).where(DatasetRow.owner_user_id == owner_user_id)
                )
            ).all()
            return [to_embedded_dataset(row) for row in rows]

    async def query_candidates(self, request: DatasetRecordScan) -> list[DatasetRecord]:
        """Return records in the created_at range, newest first, capped at scan_limit."""
        return await query_records(self._session_factory, request)
