"""Public SQL adapter over exchange reads, commands and guarded settlement."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.dialogs.exchange_commands import ExchangeCommands
from octoforge_core.dialogs.exchange_queries import ExchangeQueries
from octoforge_core.dialogs.exchange_settlement import settle_owned
from octoforge_core.dialogs.requests import ExchangeSettlement
from octoforge_core.dialogs.types import Exchange, ExchangeList, ExchangeStatus


class SqlAlchemyExchangeRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory
        self._commands = ExchangeCommands(session_factory)
        self._queries = ExchangeQueries(session_factory)

    async def create(
        self,
        dialog_id: str,
        title: str,
        status: ExchangeStatus | None = None,
    ) -> Exchange:
        return await self._commands.create(dialog_id, title, status)

    async def get(self, exchange_id: str) -> Exchange:
        return await self._queries.get(exchange_id)

    async def find_collecting(self, dialog_id: str) -> Exchange | None:
        return await self._queries.find_collecting(dialog_id)

    async def list_stale_collecting(self, quiet_seconds: float) -> ExchangeList:
        return await self._queries.list_stale_collecting(quiet_seconds)

    async def touch(self, exchange_id: str) -> None:
        await self._commands.touch(exchange_id)

    async def set_title(self, exchange_id: str, title: str) -> None:
        await self._commands.set_title(exchange_id, title)

    async def list_live(self, dialog_id: str) -> ExchangeList:
        return await self._queries.list_live(dialog_id)

    async def list_unowned_open(self, dialog_id: str | None = None) -> ExchangeList:
        return await self._queries.list_unowned_open(dialog_id)

    async def list_stranded_dialog_ids(self) -> list[str]:
        return await self._queries.list_stranded_dialog_ids()

    async def reopen_and_list_stranded(self, dialog_id: str) -> tuple[int, ExchangeList]:
        return await self._queries.reopen_and_list_stranded(dialog_id)

    async def set_status(
        self,
        exchange_id: str,
        status: ExchangeStatus,
        pending_question: str | None = None,
    ) -> None:
        await self._commands.set_status(exchange_id, status, pending_question)

    async def settle_owned(self, request: ExchangeSettlement) -> Exchange | None:
        return await settle_owned(self._sessions, request)

    async def delete_for_dialog(self, dialog_id: str) -> None:
        await self._commands.delete_for_dialog(dialog_id)
