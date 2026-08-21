"""Admin listings for instructions, datasets, records and memories."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.admin._knowledge_rows import (
    to_dataset,
    to_instruction,
    to_memory,
    to_record,
)
from octoforge_core.admin._page import run_page
from octoforge_core.admin.requests import PageRequest
from octoforge_core.admin.types import Page
from octoforge_core.datasets.api import Dataset, DatasetRecord
from octoforge_core.datasets.models import DatasetRecordRow, DatasetRow
from octoforge_core.instructions.api import Instruction, InstructionType
from octoforge_core.instructions.models import InstructionRow
from octoforge_core.memory.api import Memory


async def list_instructions(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
    query: str | None,
) -> Page[Instruction]:
    filters = [InstructionRow.type != InstructionType.MEMORY.value]
    if query:
        filters.append(InstructionRow.title.ilike(f"%{query}%"))
    statement = (
        select(InstructionRow)
        .where(*filters)
        .order_by(InstructionRow.type, InstructionRow.title)
        .limit(page.limit)
        .offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(InstructionRow).where(*filters),
    )
    return Page(tuple(to_instruction(row) for row in rows), total, page.limit, page.offset)


async def list_datasets(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[Dataset]:
    statement = (
        select(DatasetRow)
        .order_by(DatasetRow.owner_user_id, DatasetRow.name)
        .limit(page.limit)
        .offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(DatasetRow),
    )
    return Page(tuple(to_dataset(row) for row in rows), total, page.limit, page.offset)


async def list_records(
    session_factory: async_sessionmaker[AsyncSession],
    dataset_id: str,
    page: PageRequest,
) -> Page[DatasetRecord]:
    statement = (
        select(DatasetRecordRow)
        .where(DatasetRecordRow.dataset_id == dataset_id)
        .order_by(DatasetRecordRow.created_at.desc(), DatasetRecordRow.id)
        .limit(page.limit)
        .offset(page.offset)
    )
    counter = (
        select(func.count())
        .select_from(DatasetRecordRow)
        .where(DatasetRecordRow.dataset_id == dataset_id)
    )
    rows, total = await run_page(session_factory, statement, counter)
    return Page(tuple(to_record(row) for row in rows), total, page.limit, page.offset)


async def list_memories(
    session_factory: async_sessionmaker[AsyncSession],
    page: PageRequest,
) -> Page[Memory]:
    memory_only = InstructionRow.type == InstructionType.MEMORY.value
    statement = (
        select(InstructionRow)
        .where(memory_only)
        .order_by(InstructionRow.updated_at.desc(), InstructionRow.title)
        .limit(page.limit)
        .offset(page.offset)
    )
    rows, total = await run_page(
        session_factory,
        statement,
        select(func.count()).select_from(InstructionRow).where(memory_only),
    )
    return Page(tuple(to_memory(row) for row in rows), total, page.limit, page.offset)
