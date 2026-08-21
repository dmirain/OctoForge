"""Admin usage-ledger events and aggregated reports."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin._page import run_page
from octoforge_core.admin.account_types import UsageEventOverview, UsageReportRow
from octoforge_core.admin.requests import PageRequest
from octoforge_core.admin.types import Page
from octoforge_core.db.unit_of_work import read_session
from octoforge_core.tariffs.models import UsageEventRow
from octoforge_core.time import utc_now


async def list_usage_events(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[UsageEventOverview]:
    statement = (
        select(UsageEventRow)
        .order_by(UsageEventRow.created_at.desc())
        .limit(page.limit)
        .offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(UsageEventRow),
    )
    items = tuple(
        UsageEventOverview(
            row.user_id,
            row.kind,
            row.origin,
            row.prompt_tokens,
            row.completion_tokens,
            row.quantity,
            row.dialog_id,
            row.exchange_id,
            row.task_id,
            row.created_at,
        )
        for row in rows
    )
    return Page(items, total, page.limit, page.offset)


async def usage_report(
    session_factory: async_sessionmaker[AsyncSession],
    days: int,
) -> list[UsageReportRow]:
    cutoff = utc_now() - timedelta(days=max(1, days))
    statement = (
        select(
            UsageEventRow.user_id,
            UsageEventRow.kind,
            UsageEventRow.origin,
            func.sum(UsageEventRow.prompt_tokens),
            func.sum(UsageEventRow.completion_tokens),
            func.sum(UsageEventRow.quantity),
            func.count(),
        )
        .where(UsageEventRow.created_at >= cutoff)
        .group_by(UsageEventRow.user_id, UsageEventRow.kind, UsageEventRow.origin)
        .order_by(UsageEventRow.user_id, UsageEventRow.kind, UsageEventRow.origin)
    )
    async with read_session(session_factory) as session:
        rows = (await session.execute(statement)).all()
    return [
        UsageReportRow(user, kind, origin, prompt or 0, completion or 0, quantity or 0, events)
        for user, kind, origin, prompt, completion, quantity, events in rows
    ]
