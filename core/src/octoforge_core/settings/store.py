"""SQL store of the settings module."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.settings.api import Setting, normalize_key, normalize_value
from octoforge_core.settings.models import AppSettingRow
from octoforge_core.time import utc_now


class SqlAlchemySettingsStore:
    """Installation settings; writes come from the operator console only."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> str | None:
        key = normalize_key(key)
        async with read_session(self._session_factory) as session:
            value: str | None = await session.scalar(
                select(AppSettingRow.value).where(AppSettingRow.key == key)
            )
            return value

    async def put(self, key: str, value: str) -> Setting:
        key = normalize_key(key)
        value = normalize_value(value)
        async with write_session(self._session_factory) as session:
            row = await session.get(AppSettingRow, key)
            if row is None:
                row = AppSettingRow(key=key, value=value)
                session.add(row)
            else:
                row.value = value
                row.updated_at = utc_now()
            await session.flush()
            return _to_setting(row)

    async def delete(self, key: str) -> None:
        key = normalize_key(key)
        async with write_session(self._session_factory) as session:
            row = await session.get(AppSettingRow, key)
            if row is not None:
                await session.delete(row)

    async def list(self) -> list[Setting]:
        async with read_session(self._session_factory) as session:
            rows = await session.scalars(select(AppSettingRow).order_by(AppSettingRow.key))
            return [_to_setting(row) for row in rows.all()]


def _to_setting(row: AppSettingRow) -> Setting:
    return Setting(key=row.key, value=row.value, updated_at=row.updated_at)
