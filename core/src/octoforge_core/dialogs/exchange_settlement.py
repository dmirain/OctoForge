"""Atomic exchange settlement guarded by newest-task ownership."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import ScalarSelect

from octoforge_core.db.unit_of_work import write_session
from octoforge_core.dialogs._rows import to_exchange
from octoforge_core.dialogs.models import ExchangeRow
from octoforge_core.dialogs.requests import ExchangeSettlement
from octoforge_core.dialogs.types import LIVE_EXCHANGE_STATUSES, Exchange, ExchangeStatus
from octoforge_core.tasks.models import TaskRow


async def settle_owned(
    session_factory: async_sessionmaker[AsyncSession],
    request: ExchangeSettlement,
) -> Exchange | None:
    conditions = [
        ExchangeRow.id == request.exchange_id,
        _newest_task_of(request.exchange_id) == request.task_id,
        ExchangeRow.status.in_(tuple(item.value for item in LIVE_EXCHANGE_STATUSES)),
    ]
    if request.keep_if_awaiting:
        conditions.append(ExchangeRow.status != ExchangeStatus.AWAITING_USER.value)
    async with write_session(session_factory) as session:
        row = (
            await session.scalars(
                update(ExchangeRow)
                .where(*conditions)
                .values(status=request.status.value, pending_question=None)
                .returning(ExchangeRow)
            )
        ).first()
        return None if row is None else to_exchange(row)


def _newest_task_of(exchange_id: str) -> ScalarSelect[str]:
    return (
        select(TaskRow.id)
        .where(TaskRow.exchange_id == exchange_id)
        .order_by(TaskRow.created_at.desc(), TaskRow.id.desc())
        .limit(1)
        .scalar_subquery()
    )
