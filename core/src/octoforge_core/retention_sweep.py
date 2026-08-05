"""The sweep that applies a `RetentionPolicy`, with the guards that make it safe.

Deleting history unattended is the kind of feature that is either correct or a
disaster, so every rule here is a refusal:

- **Nothing runs unless an operator configured a limit.** The default policy
  deletes nothing, and the sweep returns immediately.
- **A message at or after its dialog's compaction boundary is never deleted.**
  Those are the rows the runner reloads to rebuild its narrative after a
  restart; removing one would silently change what the agent believes happened.
  Only history that already lives behind a summary can age out.
- **A live exchange is never deleted**, whatever its age. An obligation that is
  still open, collecting or awaiting the user is work in flight, and an old
  timestamp on it usually means it has been *neglected*, which is the last
  thing to clean up silently.
- **An undelivered task is never deleted.** Its result has not reached the user
  yet; age is not consent to drop it.

Age-based rather than count-based on purpose: a quiet week must never empty a
dialog, which a "keep the last N" rule would happily do.
"""

import logging
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Delete, delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.context.models import SummaryRow
from octoforge_core.dialogs.api import LIVE_EXCHANGE_STATUSES
from octoforge_core.dialogs.models import ExchangeRow, MessageRow
from octoforge_core.retention import RetentionOutcome, RetentionPolicy
from octoforge_core.tariffs.models import UsageEventRow
from octoforge_core.tasks.models import TaskRow

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetentionSweeper:
    """Applies a retention policy to the transcript-shaped tables."""

    session_factory: async_sessionmaker[AsyncSession]
    policy: RetentionPolicy

    async def sweep(self) -> RetentionOutcome:
        """Delete what the policy allows; return what went. A no-op when unset."""
        if not self.policy.enabled():
            return RetentionOutcome()
        outcome = RetentionOutcome(
            messages=await self._sweep_messages(),
            exchanges=await self._sweep_exchanges(),
            tasks=await self._sweep_tasks(),
            usage=await self._sweep_usage(),
        )
        if outcome.total():
            logger.info("retention sweep removed %s", outcome.describe())
        return outcome

    async def _sweep_messages(self) -> int:
        """Delete old messages that already live behind a compaction summary.

        The boundary check is the guard that matters: everything at or after it
        is what a restarting runner reloads as its narrative.
        """
        cutoff = self.policy.cutoff(self.policy.messages_days)
        if cutoff is None:
            return 0
        async with self.session_factory() as session:
            boundary = (
                select(SummaryRow.seq_to)
                .where(SummaryRow.dialog_id == MessageRow.dialog_id)
                .order_by(SummaryRow.seq_to.desc())
                .limit(1)
                .scalar_subquery()
            )
            statement = delete(MessageRow).where(
                MessageRow.created_at < cutoff,
                MessageRow.seq <= boundary,
            )
            return await self._run(session, statement)

    async def _sweep_exchanges(self) -> int:
        """Delete old settled exchanges; anything still live stays."""
        cutoff = self.policy.cutoff(self.policy.exchanges_days)
        if cutoff is None:
            return 0
        live = [status.value for status in LIVE_EXCHANGE_STATUSES]
        async with self.session_factory() as session:
            statement = delete(ExchangeRow).where(
                ExchangeRow.created_at < cutoff,
                ExchangeRow.status.not_in(live),
            )
            return await self._run(session, statement)

    async def _sweep_tasks(self) -> int:
        """Delete old tasks whose result already reached the user."""
        cutoff = self.policy.cutoff(self.policy.tasks_days)
        if cutoff is None:
            return 0
        async with self.session_factory() as session:
            statement = delete(TaskRow).where(
                TaskRow.created_at < cutoff,
                TaskRow.delivered_at.is_not(None),
            )
            return await self._run(session, statement)

    async def _sweep_usage(self) -> int:
        """Delete old usage events; limit checks only ever read the current day."""
        cutoff = self.policy.cutoff(self.policy.usage_days)
        if cutoff is None:
            return 0
        async with self.session_factory() as session:
            statement = delete(UsageEventRow).where(UsageEventRow.created_at < cutoff)
            return await self._run(session, statement)

    @staticmethod
    async def _run(session: AsyncSession, statement: Delete) -> int:
        result = cast(CursorResult[Any], await session.execute(statement))
        await session.commit()
        return int(result.rowcount)
