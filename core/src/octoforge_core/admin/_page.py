"""Execute one admin listing and its total-count query."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session


async def run_page(
    session_factory: async_sessionmaker[AsyncSession],
    statement: Select[Any],
    counter: Select[Any],
) -> tuple[Sequence[Any], int]:
    async with read_session(session_factory) as session:
        rows = (await session.scalars(statement)).all()
        total = int((await session.execute(counter)).scalar_one())
    return rows, total
