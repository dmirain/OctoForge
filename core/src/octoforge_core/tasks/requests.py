"""Internal task persistence commands."""

from dataclasses import dataclass

from octoforge_core.tasks.api import TaskStatus


@dataclass(frozen=True, slots=True)
class TaskCompletion:
    task_id: str
    status: TaskStatus
    result: str | None = None
    error: str | None = None
    delivered: bool = False
