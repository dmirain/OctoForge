"""Single-statement SQL transition to a terminal task state."""

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import write_session
from octoforge_core.tasks._rows import to_task
from octoforge_core.tasks.api import Task, TaskNotFoundError
from octoforge_core.tasks.models import TaskRow
from octoforge_core.tasks.requests import TaskCompletion
from octoforge_core.time import utc_now


async def finish_task(
    session_factory: async_sessionmaker[AsyncSession],
    completion: TaskCompletion,
) -> Task:
    now = utc_now()
    values: dict[str, Any] = {"status": completion.status.value, "finished_at": now}
    if completion.result is not None:
        values["result"] = completion.result
    if completion.error is not None:
        values["error"] = completion.error
    if completion.delivered:
        values["delivered_at"] = now
    async with write_session(session_factory) as session:
        row = (
            await session.scalars(
                update(TaskRow)
                .where(TaskRow.id == completion.task_id)
                .values(**values)
                .returning(TaskRow)
            )
        ).first()
        if row is None:
            raise TaskNotFoundError(completion.task_id)
        return to_task(row)
