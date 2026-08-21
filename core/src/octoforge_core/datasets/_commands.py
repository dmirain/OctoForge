"""SQL command for creating a dataset under its uniqueness invariant."""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.datasets._rows import to_dataset
from octoforge_core.datasets.models import DatasetRow
from octoforge_core.datasets.requests import DatasetDefinition
from octoforge_core.datasets.types import Dataset, DatasetExistsError
from octoforge_core.datasets.validation import dump_schema

FIRST_VERSION = 1


async def create_dataset(
    session_factory: async_sessionmaker[AsyncSession],
    definition: DatasetDefinition,
    embedding: tuple[float, ...],
) -> Dataset:
    """Insert one descriptor, using commit as the uniqueness-race detector."""
    async with session_factory() as session:
        row = DatasetRow(
            id=uuid.uuid4().hex,
            owner_user_id=definition.owner_user_id,
            name=definition.name,
            description=definition.description,
            schema=dump_schema(definition.schema),
            usage_notes=definition.usage_notes,
            retention=definition.retention,
            embedding=list(embedding),
            version=FIRST_VERSION,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            raise DatasetExistsError(f"dataset '{definition.name}' already exists") from exc
        return to_dataset(row)
