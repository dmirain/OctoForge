"""Admin dialog overview and narrative message listings."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import ScalarSelect

from octoforge_core.admin._conversation_rows import to_message
from octoforge_core.admin._page import run_page
from octoforge_core.admin.requests import PageRequest
from octoforge_core.admin.types import DialogOverview, MessageRecord, Page
from octoforge_core.db.unit_of_work import read_session
from octoforge_core.dialogs.api import ACTIVITY_WINDOW
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.tasks.models import TaskRow
from octoforge_core.time import utc_now


async def list_dialogs(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[DialogOverview]:
    user_count = _message_count("user")
    agent_count = _message_count("assistant")
    task_count = (
        select(func.count())
        .select_from(TaskRow)
        .where(TaskRow.dialog_id == DialogRow.id)
        .scalar_subquery()
    )
    last_message = (
        select(func.max(MessageRow.created_at))
        .where(MessageRow.dialog_id == DialogRow.id)
        .scalar_subquery()
    )
    last_user = (
        select(func.max(MessageRow.created_at))
        .where(MessageRow.dialog_id == DialogRow.id, MessageRow.role == "user")
        .scalar_subquery()
    )
    recent_user_count = (
        select(func.count())
        .select_from(MessageRow)
        .where(
            MessageRow.dialog_id == DialogRow.id,
            MessageRow.role == "user",
            MessageRow.created_at >= utc_now() - ACTIVITY_WINDOW,
        )
        .scalar_subquery()
    )
    statement = (
        select(
            DialogRow,
            user_count,
            agent_count,
            task_count,
            last_message,
            last_user,
            recent_user_count,
        )
        .order_by(last_message.desc().nullslast(), DialogRow.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    async with read_session(session_factory) as session:
        rows = (await session.execute(statement)).all()
        total = int(await session.scalar(select(func.count()).select_from(DialogRow)) or 0)
    items = tuple(
        DialogOverview(
            row[0].id,
            row[0].user_id,
            row[0].channel,
            row[0].created_at,
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
        )
        for row in rows
    )
    return Page(items, total, page.limit, page.offset)


async def list_messages(
    session_factory: async_sessionmaker[AsyncSession],
    dialog_id: str,
    page: PageRequest,
) -> Page[MessageRecord]:
    statement = (
        select(MessageRow)
        .where(MessageRow.dialog_id == dialog_id)
        .order_by(MessageRow.seq)
        .limit(page.limit)
        .offset(page.offset)
    )
    counter = select(func.count()).select_from(MessageRow).where(MessageRow.dialog_id == dialog_id)
    rows, total = await run_page(session_factory, statement, counter)
    return Page(tuple(to_message(row) for row in rows), total, page.limit, page.offset)


def _message_count(role: str) -> ScalarSelect[int]:
    return (
        select(func.count())
        .select_from(MessageRow)
        .where(MessageRow.dialog_id == DialogRow.id, MessageRow.role == role)
        .scalar_subquery()
    )
