"""Insert-only SQL usage ledger and window aggregation."""

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session, write_session
from octoforge_core.tariffs.models import UsageEventRow
from octoforge_core.tariffs.usage_types import UsageEvent, UsageKind, UsageTotals
from octoforge_core.time import utc_now


class SqlAlchemyUsageMeter:
    """Append concurrent usage events and aggregate indexed time windows."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, event: UsageEvent) -> None:
        async with write_session(self._session_factory) as session:
            session.add(
                UsageEventRow(
                    id=uuid.uuid4().hex,
                    user_id=event.user_id,
                    kind=event.kind.value,
                    origin=event.origin.value,
                    prompt_tokens=event.prompt_tokens,
                    completion_tokens=event.completion_tokens,
                    quantity=event.quantity,
                    dialog_id=event.dialog_id,
                    exchange_id=event.exchange_id,
                    task_id=event.task_id,
                    created_at=event.created_at or utc_now(),
                )
            )

    async def totals_since(self, user_id: str, since: datetime) -> UsageTotals:
        def counted(kind: UsageKind) -> ColumnElement[int]:
            return func.sum(case((UsageEventRow.kind == kind.value, UsageEventRow.quantity)))

        async with read_session(self._session_factory) as session:
            row = (
                await session.execute(
                    select(
                        func.sum(UsageEventRow.prompt_tokens),
                        func.sum(UsageEventRow.completion_tokens),
                        counted(UsageKind.USER_MESSAGE),
                        counted(UsageKind.LLM_ANSWER),
                    ).where(
                        UsageEventRow.user_id == user_id,
                        UsageEventRow.created_at >= since,
                    )
                )
            ).one()
        prompt, completion, user_messages, answers = row
        return UsageTotals(
            prompt_tokens=prompt or 0,
            completion_tokens=completion or 0,
            user_messages=user_messages or 0,
            assistant_messages=answers or 0,
        )
