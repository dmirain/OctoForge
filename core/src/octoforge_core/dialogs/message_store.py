"""Public SQL adapter over message writing, reads and activity projections."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.dialogs.message_activity import MessageActivityQueries
from octoforge_core.dialogs.message_queries import MessageQueries
from octoforge_core.dialogs.message_writer import MessageWriter
from octoforge_core.dialogs.requests import MessageAppend
from octoforge_core.dialogs.types import MessageStatsList, UserActivityList
from octoforge_core.domain import ChatMessage


class SqlAlchemyMessageRepository:
    """Ordered message log with race-safe appends and derived activity."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._writer = MessageWriter(session_factory)
        self._queries = MessageQueries(session_factory)
        self._activity = MessageActivityQueries(session_factory)

    async def append(self, request: MessageAppend) -> str:
        return await self._writer.append(request)

    async def append_pair(self, dialog_id: str, first: ChatMessage, second: ChatMessage) -> None:
        await self._writer.append_pair(dialog_id, first, second)

    async def find_by_client_id(self, dialog_id: str, client_message_id: str) -> bool:
        return await self._queries.find_by_client_id(dialog_id, client_message_id)

    async def list_hot_slice(self, dialog_id: str) -> list[ChatMessage]:
        return await self._queries.list_hot_slice(dialog_id)

    async def list_after(self, dialog_id: str, after_seq: int) -> list[ChatMessage]:
        return await self._queries.list_after(dialog_id, after_seq)

    async def list(self, dialog_id: str) -> list[ChatMessage]:
        return await self._queries.list(dialog_id)

    async def set_exchange(self, message_id: str, exchange_id: str) -> None:
        await self._queries.set_exchange(message_id, exchange_id)

    async def last_activity_by_channel(self, channel: str) -> dict[str, datetime]:
        return await self._activity.last_activity_by_channel(channel)

    async def stats_by_channel(self, channel: str) -> MessageStatsList:
        return await self._activity.stats_by_channel(channel)

    async def user_activity_by_channel(self, channel: str, since: datetime) -> UserActivityList:
        return await self._activity.user_activity_by_channel(channel, since)
