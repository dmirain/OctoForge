"""SQL implementation of the admin read model.

Every listing is `SELECT … LIMIT/OFFSET` plus a `count()` for the pager, and
nothing here writes. The row→DTO mappings mirror the ones each owning module
keeps private (`cron/store.py:_to_cron_job` and friends): the alternative would
be exporting them, which would turn an internal detail of every module into
public API for one console.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin.api import (
    DialogOverview,
    MessageRecord,
    Page,
    Totals,
)
from octoforge_core.context.api import DialogueSummary
from octoforge_core.context.models import SummaryRow
from octoforge_core.cron.api import CronJob
from octoforge_core.cron.models import CronJobRow
from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.datasets.validation import parse_schema
from octoforge_core.db.models import DialogRow, MessageRow, TaskRow
from octoforge_core.instructions.api import Instruction, InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.memory.api import Memory
from octoforge_core.memory.models import MemoryRow
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus


class SqlAlchemyAdminStore:
    """Read-only cross-user views over every table of the schema."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def totals(self) -> Totals:
        async with self._session_factory() as session:
            counts = {
                name: await _count(session, select(func.count()).select_from(model))
                for name, model in (
                    ("dialogs", DialogRow),
                    ("messages", MessageRow),
                    ("tasks", TaskRow),
                    ("cron_jobs", CronJobRow),
                    ("instructions", InstructionRow),
                    ("datasets", DatasetRow),
                    ("dataset_records", DatasetRecordRow),
                    ("memories", MemoryRow),
                    ("dialog_summaries", SummaryRow),
                )
            }
        return Totals(**counts)

    async def list_dialogs(self, limit: int, offset: int) -> Page[DialogOverview]:
        """Dialogs with their counters, most recently active first.

        The counters are correlated scalar subqueries rather than joins: a join
        would multiply rows across messages and tasks and need grouping to undo.
        """
        message_count = (
            select(func.count())
            .select_from(MessageRow)
            .where(MessageRow.dialog_id == DialogRow.id)
            .scalar_subquery()
        )
        task_count = (
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.dialog_id == DialogRow.id)
            .scalar_subquery()
        )
        last_message_at = (
            select(func.max(MessageRow.created_at))
            .where(MessageRow.dialog_id == DialogRow.id)
            .scalar_subquery()
        )
        statement = (
            select(DialogRow, message_count, task_count, last_message_at)
            .order_by(DialogRow.updated_at.desc(), DialogRow.id)
            .limit(limit)
            .offset(offset)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            total = await _count(session, select(func.count()).select_from(DialogRow))
        items = tuple(
            DialogOverview(
                id=row[0].id,
                user_id=row[0].user_id,
                channel=row[0].channel,
                created_at=row[0].created_at,
                updated_at=row[0].updated_at,
                message_count=row[1],
                task_count=row[2],
                last_message_at=row[3],
            )
            for row in rows
        )
        return Page(items=items, total=total, limit=limit, offset=offset)

    async def list_messages(self, dialog_id: str, limit: int, offset: int) -> Page[MessageRecord]:
        statement = (
            select(MessageRow)
            .where(MessageRow.dialog_id == dialog_id)
            .order_by(MessageRow.seq)
            .limit(limit)
            .offset(offset)
        )
        counter = (
            select(func.count()).select_from(MessageRow).where(MessageRow.dialog_id == dialog_id)
        )
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_message_record(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_tasks(
        self,
        limit: int,
        offset: int,
        status: str | None = None,
        kind: str | None = None,
    ) -> Page[Task]:
        filters = []
        if status is not None:
            filters.append(TaskRow.status == status)
        if kind is not None:
            filters.append(TaskRow.kind == kind)
        statement = (
            select(TaskRow)
            .where(*filters)
            .order_by(TaskRow.created_at.desc(), TaskRow.id)
            .limit(limit)
            .offset(offset)
        )
        counter = select(func.count()).select_from(TaskRow).where(*filters)
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_task(row) for row in rows), total=total, limit=limit, offset=offset
        )

    async def list_cron_jobs(self, limit: int, offset: int) -> Page[CronJob]:
        statement = select(CronJobRow).order_by(CronJobRow.next_fire_at).limit(limit).offset(offset)
        counter = select(func.count()).select_from(CronJobRow)
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_cron_job(row) for row in rows), total=total, limit=limit, offset=offset
        )

    async def list_instructions(
        self,
        limit: int,
        offset: int,
        query: str | None = None,
    ) -> Page[Instruction]:
        filters = [] if not query else [InstructionRow.title.ilike(f"%{query}%")]
        statement = (
            select(InstructionRow)
            .where(*filters)
            .order_by(InstructionRow.type, InstructionRow.title)
            .limit(limit)
            .offset(offset)
        )
        counter = select(func.count()).select_from(InstructionRow).where(*filters)
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_instruction(row) for row in rows),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_datasets(self, limit: int, offset: int) -> Page[Dataset]:
        statement = (
            select(DatasetRow)
            .order_by(DatasetRow.owner_user_id, DatasetRow.name)
            .limit(limit)
            .offset(offset)
        )
        counter = select(func.count()).select_from(DatasetRow)
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_dataset(row) for row in rows), total=total, limit=limit, offset=offset
        )

    async def list_dataset_records(
        self,
        dataset_id: str,
        limit: int,
        offset: int,
    ) -> Page[DatasetRecord]:
        statement = (
            select(DatasetRecordRow)
            .where(DatasetRecordRow.dataset_id == dataset_id)
            .order_by(DatasetRecordRow.created_at.desc(), DatasetRecordRow.id)
            .limit(limit)
            .offset(offset)
        )
        counter = (
            select(func.count())
            .select_from(DatasetRecordRow)
            .where(DatasetRecordRow.dataset_id == dataset_id)
        )
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_record(row) for row in rows), total=total, limit=limit, offset=offset
        )

    async def list_memories(self, limit: int, offset: int) -> Page[Memory]:
        statement = (
            select(MemoryRow)
            .order_by(MemoryRow.updated_at.desc(), MemoryRow.key)
            .limit(limit)
            .offset(offset)
        )
        counter = select(func.count()).select_from(MemoryRow)
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_memory(row) for row in rows), total=total, limit=limit, offset=offset
        )

    async def list_summaries(self, limit: int, offset: int) -> Page[DialogueSummary]:
        statement = (
            select(SummaryRow)
            .order_by(SummaryRow.created_at.desc(), SummaryRow.id)
            .limit(limit)
            .offset(offset)
        )
        counter = select(func.count()).select_from(SummaryRow)
        rows, total = await self._page(statement, counter)
        return Page(
            items=tuple(_to_summary(row) for row in rows), total=total, limit=limit, offset=offset
        )

    async def _page(
        self,
        statement: Select[Any],
        counter: Select[Any],
    ) -> tuple[Sequence[Any], int]:
        """Run a listing and its counter on one session."""
        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            total = await _count(session, counter)
        return rows, total


async def _count(session: AsyncSession, statement: Select[Any]) -> int:
    return int((await session.execute(statement)).scalar_one())


def _to_message_record(row: MessageRow) -> MessageRecord:
    return MessageRecord(
        id=row.id,
        dialog_id=row.dialog_id,
        seq=row.seq,
        role=row.role,
        content=row.content,
        task_id=row.task_id,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        created_at=row.created_at,
    )


def _to_task(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        dialog_id=row.dialog_id,
        user_id=row.user_id,
        channel=row.channel,
        kind=TaskKind(row.kind),
        title=row.title,
        input=row.input,
        status=TaskStatus(row.status),
        result=row.result,
        error=row.error,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        delivered_at=row.delivered_at,
    )


def _to_cron_job(row: CronJobRow) -> CronJob:
    return CronJob(
        id=row.id,
        user_id=row.user_id,
        channel=row.channel,
        title=row.title,
        schedule=row.schedule,
        timezone=row.timezone,
        prompt=row.prompt,
        enabled=row.enabled,
        next_fire_at=row.next_fire_at,
        last_fire_at=row.last_fire_at,
        claimed_by=row.claimed_by,
        claimed_at=row.claimed_at,
        created_at=row.created_at,
        one_shot=row.one_shot,
        last_status=None if row.last_status is None else TaskStatus(row.last_status),
        last_error=row.last_error,
        retry_count=row.retry_count,
    )


def _to_instruction(row: InstructionRow) -> Instruction:
    return Instruction(
        id=row.id,
        type=InstructionType(row.type),
        title=row.title,
        content=row.content,
        tags=tuple(row.tags),
        version=row.version,
        usage_count=row.usage_count,
        success_count=row.success_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        system=row.system,
        owner_id=row.owner_id,
    )


def _to_dataset(row: DatasetRow) -> Dataset:
    return Dataset(
        id=row.id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        description=row.description,
        schema=parse_schema(row.schema),
        usage_notes=row.usage_notes,
        retention=row.retention,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_record(row: DatasetRecordRow) -> DatasetRecord:
    return DatasetRecord(
        id=row.id,
        dataset_id=row.dataset_id,
        owner_user_id=row.owner_user_id,
        payload=row.payload,
        created_at=row.created_at,
    )


def _to_memory(row: MemoryRow) -> Memory:
    return Memory(
        id=row.id,
        user_id=row.user_id,
        key=row.key,
        content=row.content,
        tags=tuple(row.tags),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_summary(row: SummaryRow) -> DialogueSummary:
    return DialogueSummary(
        id=row.id,
        dialog_id=row.dialog_id,
        seq_from=row.seq_from,
        seq_to=row.seq_to,
        topics=tuple(row.topics),
        content=row.content,
        created_at=row.created_at,
    )
