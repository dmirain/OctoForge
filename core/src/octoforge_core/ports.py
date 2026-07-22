"""Ports (protocols) used by the core services."""

from collections.abc import AsyncIterator
from typing import Protocol

from octoforge_core.domain import ChatMessage
from octoforge_core.llm.events import StreamEvent
from octoforge_core.llm.usage import Completion
from octoforge_core.skills.base import SkillSpec
from octoforge_core.tasks.models import Task

# Inside TaskStore the method named `list` shadows the builtin in class-scope
# annotations, so list-returning signatures defined after it alias it here.
TaskList = list[Task]


class LLMClient(Protocol):
    """Async chat-completion client port."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> Completion:
        """Return the assistant reply and its usage for the given conversation."""
        ...

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the assistant reply as events."""
        ...


class TaskStore(Protocol):
    """Persistence port for background tasks."""

    async def add(self, task: Task) -> None:
        """Store a new task."""
        ...

    async def get(self, task_id: str) -> Task:
        """Return the task by id or raise TaskNotFoundError."""
        ...

    async def list(self, dialog_id: str) -> list[Task]:
        """Return tasks of one dialog."""
        ...

    async def mark_running(self, task: Task) -> None:
        """Mark the task as running."""
        ...

    async def mark_done(self, task: Task, result: str) -> None:
        """Mark the task as done with a result."""
        ...

    async def mark_failed(self, task: Task, error: str) -> None:
        """Mark the task as failed with an error."""
        ...

    async def cancel(self, task_id: str) -> None:
        """Mark the task as cancelled or raise TaskNotFoundError."""
        ...

    async def is_cancelled(self, task_id: str) -> bool:
        """Return True when the task exists and is cancelled."""
        ...

    async def count_active(self, dialog_id: str) -> int:
        """Return the number of pending or running tasks of the dialog."""
        ...

    async def mark_delivered(self, task_id: str) -> None:
        """Mark the task result as delivered to its dialog."""
        ...

    async def fail_orphaned(self, error: str) -> TaskList:
        """Fail every PENDING/RUNNING task with `error`; return the affected tasks.

        Their in-memory executors (actor pump processes) died with the previous
        service instance, so they can never finish on their own.
        """
        ...

    async def list_undelivered(self) -> TaskList:
        """Return finished (DONE/FAILED) tasks whose result was never delivered."""
        ...
