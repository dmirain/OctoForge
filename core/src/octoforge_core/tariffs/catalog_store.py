"""SQL persistence for the operator-managed tariff catalog."""

import uuid
from dataclasses import asdict

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.tariffs._assignments import assign, assignments, tariff_for_user
from octoforge_core.tariffs._rows import find_tariff, to_tariff
from octoforge_core.tariffs.models import TariffRow, UserTariffRow
from octoforge_core.tariffs.policy import normalize_code, normalize_feature, normalize_title
from octoforge_core.tariffs.types import (
    Tariff,
    TariffDefinition,
    TariffInUseError,
    TariffLimits,
    TariffNotFoundError,
)
from octoforge_core.time import utc_now


class SqlAlchemyTariffStore:
    """Create plans and maintain their user assignments."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def put(self, definition: TariffDefinition) -> Tariff:
        code = normalize_code(definition.code)
        title = normalize_title(definition.title)
        caps = (definition.limits or TariffLimits()).normalized()
        async with write_session(self._session_factory) as session:
            row = await find_tariff(session, code)
            if row is None:
                row = TariffRow(id=uuid.uuid4().hex, code=code, title=title)
                session.add(row)
            else:
                row.title = title
                row.updated_at = utc_now()
            row.features = sorted({normalize_feature(feature) for feature in definition.features})
            for name, value in asdict(caps).items():
                setattr(row, name, value)
            row.is_default = definition.is_default
            if definition.is_default:
                await session.execute(
                    update(TariffRow).where(TariffRow.code != code).values(is_default=False)
                )
            await session.flush()
            return to_tariff(row)

    async def list(self) -> list[Tariff]:
        async with read_session(self._session_factory) as session:
            rows = (await session.scalars(select(TariffRow).order_by(TariffRow.code))).all()
            return [to_tariff(row) for row in rows]

    async def delete(self, code: str) -> None:
        normalized = normalize_code(code)
        async with write_session(self._session_factory) as session:
            row = await find_tariff(session, normalized)
            if row is None:
                raise TariffNotFoundError(normalized)
            assigned = await session.scalar(
                select(func.count())
                .select_from(UserTariffRow)
                .where(UserTariffRow.tariff_id == row.id)
            )
            if assigned:
                raise TariffInUseError(f"tariff '{normalized}' is assigned to {assigned} user(s)")
            await session.delete(row)

    async def assign(self, user_id: str, code: str | None) -> None:
        await assign(self._session_factory, user_id, code)

    async def tariff_for_user(self, user_id: str) -> Tariff | None:
        return await tariff_for_user(self._session_factory, user_id)

    async def assignments(self) -> dict[str, str]:
        return await assignments(self._session_factory)
