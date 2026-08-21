"""Per-user message activity and author-split statistics."""

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.dialogs.types import (
    MessageStats,
    MessageStatsList,
    UserActivity,
    UserActivityList,
)
from octoforge_core.domain import MessageRole


class MessageActivityQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def last_activity_by_channel(self, channel: str) -> dict[str, datetime]:
        async with read_session(self._sessions) as session:
            rows = (
                await session.execute(
                    select(DialogRow.user_id, func.max(MessageRow.created_at))
                    .join(DialogRow, MessageRow.dialog_id == DialogRow.id)
                    .where(DialogRow.channel == channel)
                    .group_by(DialogRow.user_id)
                )
            ).all()
        return {user_id: last for user_id, last in rows if last is not None}

    async def stats_by_channel(self, channel: str) -> MessageStatsList:
        def role_count(role: MessageRole) -> ColumnElement[int]:
            return func.count(case((MessageRow.role == role.value, 1)))

        def role_chars(role: MessageRole) -> ColumnElement[int]:
            return func.coalesce(
                func.sum(case((MessageRow.role == role.value, func.length(MessageRow.content)))),
                0,
            )

        async with read_session(self._sessions) as session:
            rows = (
                await session.execute(
                    select(
                        DialogRow.user_id,
                        role_count(MessageRole.USER),
                        role_chars(MessageRole.USER),
                        role_count(MessageRole.ASSISTANT),
                        role_chars(MessageRole.ASSISTANT),
                    )
                    .join(DialogRow, MessageRow.dialog_id == DialogRow.id)
                    .where(DialogRow.channel == channel)
                    .group_by(DialogRow.user_id)
                )
            ).all()
        return [MessageStats(*row) for row in rows]

    async def user_activity_by_channel(self, channel: str, since: datetime) -> UserActivityList:
        is_user = MessageRow.role == MessageRole.USER.value
        last_written = func.max(case((is_user, MessageRow.created_at)))
        written_since = func.count(case((is_user & (MessageRow.created_at >= since), 1)))
        async with read_session(self._sessions) as session:
            rows = (
                await session.execute(
                    select(DialogRow.user_id, last_written, written_since)
                    .join(DialogRow, MessageRow.dialog_id == DialogRow.id)
                    .where(DialogRow.channel == channel)
                    .group_by(DialogRow.user_id)
                )
            ).all()
        return [UserActivity(user, _as_utc(last), count) for user, last, count in rows]


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
