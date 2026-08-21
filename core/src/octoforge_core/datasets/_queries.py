"""SQL decisions for locating dataset descriptors and scanning records."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.datasets._rows import to_record
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.datasets.requests import DatasetRecordScan
from octoforge_core.datasets.types import DatasetRecord
from octoforge_core.db.unit_of_work import read_session


async def find_dataset_row(
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


async def query_records(
    session_factory: async_sessionmaker[AsyncSession],
    request: DatasetRecordScan,
) -> list[DatasetRecord]:
    statement = select(DatasetRecordRow).where(DatasetRecordRow.dataset_id == request.dataset_id)
    if request.date_from is not None:
        statement = statement.where(DatasetRecordRow.created_at >= request.date_from)
    if request.date_to is not None:
        statement = statement.where(DatasetRecordRow.created_at <= request.date_to)
    statement = statement.order_by(
        DatasetRecordRow.created_at.desc(), DatasetRecordRow.id.desc()
    ).limit(request.limit)
    async with read_session(session_factory) as session:
        rows = (await session.scalars(statement)).all()
        return [to_record(row) for row in rows]
