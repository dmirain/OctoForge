"""Mapping and lookup of persisted tariffs."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from octoforge_core.tariffs.models import TariffRow
from octoforge_core.tariffs.types import Tariff, TariffLimits


async def find_tariff(session: AsyncSession, code: str) -> TariffRow | None:
    result = await session.scalars(select(TariffRow).where(TariffRow.code == code))
    return result.first()


def to_tariff(row: TariffRow) -> Tariff:
    return Tariff(
        id=row.id,
        code=row.code,
        title=row.title,
        features=frozenset(row.features),
        limits=TariffLimits(
            daily_tokens=row.daily_tokens,
            daily_user_messages=row.daily_user_messages,
            daily_assistant_messages=row.daily_assistant_messages,
            max_cron_jobs=row.max_cron_jobs,
            max_datasets=row.max_datasets,
            max_memory_chars=row.max_memory_chars,
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_default=row.is_default,
    )
