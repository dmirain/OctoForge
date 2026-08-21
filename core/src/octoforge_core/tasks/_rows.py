"""Mapping between task ORM rows and domain values."""

from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.tasks.models import TaskRow


def to_task_row(task: Task) -> TaskRow:
    return TaskRow(
        id=task.id,
        dialog_id=task.dialog_id,
        exchange_id=task.exchange_id,
        kind=task.kind.value,
        title=task.title,
        input=task.input,
        status=task.status.value,
        result=task.result,
        error=task.error,
        created_at=task.created_at,
        started_at=task.started_at,
        finished_at=task.finished_at,
        delivered_at=task.delivered_at,
    )


def to_task(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        dialog_id=row.dialog_id,
        exchange_id=row.exchange_id,
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
