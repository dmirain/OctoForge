"""Mapping between dataset ORM rows and domain values."""

from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.datasets.types import Dataset, DatasetRecord, EmbeddedDataset
from octoforge_core.datasets.validation import parse_schema


def to_dataset(row: DatasetRow) -> Dataset:
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


def to_record(row: DatasetRecordRow) -> DatasetRecord:
    return DatasetRecord(
        id=row.id,
        dataset_id=row.dataset_id,
        owner_user_id=row.owner_user_id,
        payload=row.payload,
        created_at=row.created_at,
    )


def to_embedded_dataset(row: DatasetRow) -> EmbeddedDataset:
    """Map a descriptor row to the search value shared with pg_store."""
    return EmbeddedDataset(dataset=to_dataset(row), embedding=tuple(row.embedding))
