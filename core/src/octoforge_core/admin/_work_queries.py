"""Admin listings for tasks, cron jobs, exchanges and summaries."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin._conversation_rows import to_cron, to_exchange, to_summary, to_task
from octoforge_core.admin._page import run_page
from octoforge_core.admin.requests import ExchangeListing, PageRequest, TaskListing
from octoforge_core.admin.types import ExchangeOverview, Page, TaskOverview
from octoforge_core.context.api import DialogueSummary
from octoforge_core.context.models import SummaryRow
from octoforge_core.cron.api import CronJob
from octoforge_core.cron.models import CronJobRow
from octoforge_core.db.unit_of_work import read_session
from octoforge_core.dialogs.models import DialogRow, ExchangeRow
from octoforge_core.tasks.models import TaskRow


async def list_tasks(
    session_factory: async_sessionmaker[AsyncSession],
    request: TaskListing,
) -> Page[TaskOverview]:
    filters = []
    if request.status is not None:
        filters.append(TaskRow.status == request.status)
    if request.kind is not None:
        filters.append(TaskRow.kind == request.kind)
    statement = (
        select(TaskRow, DialogRow.user_id, DialogRow.channel)
        .join(DialogRow, TaskRow.dialog_id == DialogRow.id)
        .where(*filters)
        .order_by(TaskRow.created_at.desc(), TaskRow.id)
        .limit(request.limit)
        .offset(request.offset)
    )
    counter = select(func.count()).select_from(TaskRow).where(*filters)
    async with read_session(session_factory) as session:
        rows = (await session.execute(statement)).all()
        total = int(await session.scalar(counter) or 0)
    items = tuple(to_task(row[0], row[1], row[2]) for row in rows)
    return Page(items, total, request.limit, request.offset)


async def list_cron_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[CronJob]:
    statement = (
        select(CronJobRow).order_by(CronJobRow.next_fire_at).limit(page.limit).offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(CronJobRow),
    )
    return Page(tuple(to_cron(row) for row in rows), total, page.limit, page.offset)


async def list_exchanges(
    session_factory: async_sessionmaker[AsyncSession],
    request: ExchangeListing,
) -> Page[ExchangeOverview]:
    filters = []
    if request.user_id is not None:
        filters.append(DialogRow.user_id == request.user_id)
    if request.status is not None:
        filters.append(ExchangeRow.status == request.status)
    statement = (
        select(ExchangeRow, DialogRow.user_id, DialogRow.channel)
        .join(DialogRow, DialogRow.id == ExchangeRow.dialog_id)
        .where(*filters)
        .order_by(ExchangeRow.updated_at.desc(), ExchangeRow.id)
        .limit(request.limit)
        .offset(request.offset)
    )
    counter = (
        select(func.count())
        .select_from(ExchangeRow)
        .join(DialogRow, DialogRow.id == ExchangeRow.dialog_id)
        .where(*filters)
    )
    async with read_session(session_factory) as session:
        rows = (await session.execute(statement)).all()
        total = int(await session.scalar(counter) or 0)
    items = tuple(to_exchange(row[0], row[1], row[2]) for row in rows)
    return Page(items, total, request.limit, request.offset)


async def list_summaries(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[DialogueSummary]:
    statement = (
        select(SummaryRow)
        .order_by(SummaryRow.created_at.desc(), SummaryRow.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(SummaryRow),
    )
    return Page(tuple(to_summary(row) for row in rows), total, page.limit, page.offset)
