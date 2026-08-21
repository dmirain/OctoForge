"""User-to-tariff assignment commands and effective-plan queries."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.tariffs._rows import find_tariff, to_tariff
from octoforge_core.tariffs.models import TariffRow, UserTariffRow
from octoforge_core.tariffs.policy import normalize_code
from octoforge_core.tariffs.types import Tariff, TariffNotFoundError
from octoforge_core.time import utc_now


async def assign(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
    code: str | None,
) -> None:
    async with write_session(session_factory) as session:
        binding = (
            await session.scalars(select(UserTariffRow).where(UserTariffRow.user_id == user_id))
        ).first()
        if code is None:
            if binding is not None:
                await session.delete(binding)
            return
        tariff = await find_tariff(session, normalize_code(code))
        if tariff is None:
            raise TariffNotFoundError(code)
        if binding is None:
            session.add(UserTariffRow(id=uuid.uuid4().hex, user_id=user_id, tariff_id=tariff.id))
        else:
            binding.tariff_id = tariff.id
            binding.assigned_at = utc_now()


async def tariff_for_user(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
) -> Tariff | None:
    async with read_session(session_factory) as session:
        row = (
            await session.scalars(
                select(TariffRow)
                .join(UserTariffRow, UserTariffRow.tariff_id == TariffRow.id)
                .where(UserTariffRow.user_id == user_id)
            )
        ).first()
        if row is None:
            row = (await session.scalars(select(TariffRow).where(TariffRow.is_default))).first()
        return None if row is None else to_tariff(row)


async def assignments(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async with read_session(session_factory) as session:
        pairs = (
            await session.execute(
                select(UserTariffRow.user_id, TariffRow.code).join(
                    TariffRow,
                    TariffRow.id == UserTariffRow.tariff_id,
                )
            )
        ).all()
        return {pair.user_id: pair.code for pair in pairs}
