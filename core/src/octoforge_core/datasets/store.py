"""SQLAlchemy storage of the datasets module.

Follows the `instructions/store.py` style: sessions come from an injected
`async_sessionmaker`, ORM rows are mapped to facade DTOs at the boundary.
Implements the `DatasetStore` port from `datasets/api.py`; the vector-search
capability (`DatasetVectorSearch`) is deliberately not implemented — this
store ranks brute-force in the process.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.datasets.api import (
    Dataset,
    DatasetExistsError,
    DatasetRecord,
    DatasetSchema,
    EmbeddedDataset,
)
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.datasets.validation import dump_schema, parse_schema
from octoforge_core.db.unit_of_work import read_session, write_session

FIRST_VERSION = 1


class SqlAlchemyDatasetStore:
    """SQL persistence for dataset descriptors and their records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(  # noqa: PLR0913, PLR0917 — flat row fields, mirroring InstructionStore.upsert
        self,
        owner_user_id: str,
        name: str,
        description: str,
        schema: DatasetSchema,
        usage_notes: str,
        retention: str,
        embedding: tuple[float, ...],
    ) -> Dataset:
        """Insert the descriptor; the (owner, name) unique constraint guards races.

        Deliberately NOT unit-of-work aware (raw `_session_factory`): the
        commit itself is the uniqueness-race detector, which inside a shared
        transaction would report the clash against the unit's other writes.
        """
        async with self._session_factory() as session:
            row = DatasetRow(
                id=uuid.uuid4().hex,
                owner_user_id=owner_user_id,
                name=name,
                description=description,
                schema=dump_schema(schema),
                usage_notes=usage_notes,
                retention=retention,
                embedding=list(embedding),
                version=FIRST_VERSION,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise DatasetExistsError(f"dataset '{name}' already exists") from exc
            return _to_dataset(row)

    async def get(self, owner_user_id: str, name: str) -> Dataset | None:
        """Return the descriptor of this owner by name, or None."""
        async with read_session(self._session_factory) as session:
            row = await _find_row(session, owner_user_id, name)
            return None if row is None else _to_dataset(row)

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
            return _to_record(row)

    async def delete(self, owner_user_id: str, name: str) -> int | None:
        """Delete the descriptor and explicitly cascade to its records.

        The cascade is an explicit DELETE because SQLite runs without
        PRAGMA foreign_keys, so the FK alone would not remove the records.
        Returns the number of deleted records, or None when no such dataset.
        """
        async with write_session(self._session_factory) as session:
            row = await _find_row(session, owner_user_id, name)
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

    async def query_candidates(
        self,
        dataset_id: str,
        date_from: datetime | None,
        date_to: datetime | None,
        scan_limit: int,
    ) -> list[DatasetRecord]:
        """Return records in the created_at range, newest first, capped at scan_limit."""
        async with read_session(self._session_factory) as session:
            statement = select(DatasetRecordRow).where(DatasetRecordRow.dataset_id == dataset_id)
            if date_from is not None:
                statement = statement.where(DatasetRecordRow.created_at >= date_from)
            if date_to is not None:
                statement = statement.where(DatasetRecordRow.created_at <= date_to)
            statement = statement.order_by(
                DatasetRecordRow.created_at.desc(), DatasetRecordRow.id.desc()
            ).limit(scan_limit)
            rows = (await session.scalars(statement)).all()
            return [_to_record(row) for row in rows]


async def _find_row(
    session: AsyncSession,
    owner_user_id: str,
    name: str,
) -> DatasetRow | None:
    result = await session.scalars(
        select(DatasetRow).where(
            DatasetRow.owner_user_id == owner_user_id,
            DatasetRow.name == name,
        )
    )
    return result.first()


def _to_dataset(row: DatasetRow) -> Dataset:
    return Dataset(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        description=row.description,
        schema=parse_schema(row.schema),
        usage_notes=row.usage_notes,
        retention=row.retention,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_record(row: DatasetRecordRow) -> DatasetRecord:
    return DatasetRecord(
        id=row.id,
        dataset_id=row.dataset_id,
        owner_user_id=row.owner_user_id,
        payload=row.payload,
        created_at=row.created_at,
    )


def to_embedded_dataset(row: DatasetRow) -> EmbeddedDataset:
    """Map a descriptor row to the search DTO (shared with `pg_store.py`)."""
    return EmbeddedDataset(dataset=_to_dataset(row), embedding=tuple(row.embedding))
