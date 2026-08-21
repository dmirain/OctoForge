"""Cross-table counts for the admin landing page."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin.account_types import Totals
from octoforge_core.context.models import SummaryRow
from octoforge_core.cron.models import CronJobRow
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.db.unit_of_work import read_session
from octoforge_core.dialogs.models import DialogRow, ExchangeRow, MessageRow
from octoforge_core.instructions.api import InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.tasks.models import TaskRow


async def totals(session_factory: async_sessionmaker[AsyncSession]) -> Totals:
    async with read_session(session_factory) as session:
        counts = {
            name: await _count(session, model)
            for name, model in (
                ("dialogs", DialogRow),
                ("messages", MessageRow),
                ("tasks", TaskRow),
                ("cron_jobs", CronJobRow),
                ("datasets", DatasetRow),
                ("dataset_records", DatasetRecordRow),
                ("dialog_summaries", SummaryRow),
                ("exchanges", ExchangeRow),
            )
        }
        counts["instructions"] = int(
            await session.scalar(
                select(func.count())
                .select_from(InstructionRow)
                .where(InstructionRow.type != InstructionType.MEMORY.value)
            )
            or 0
        )
        counts["memories"] = int(
            await session.scalar(
                select(func.count())
                .select_from(InstructionRow)
                .where(InstructionRow.type == InstructionType.MEMORY.value)
            )
            or 0
        )
    return Totals(**counts)


async def _count(session: AsyncSession, model: type[object]) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)
