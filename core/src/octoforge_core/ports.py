"""Ports (protocols) used by the core services."""

from collections.abc import AsyncIterator
from typing import Protocol

from octoforge_core.domain import ChatMessage
from octoforge_core.llm.events import StreamEvent
from octoforge_core.skills.base import SkillSpec
from octoforge_core.tasks.models import Task


class LLMClient(Protocol):
    """Async chat-completion client port."""

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        """Return the assistant reply for the given conversation."""
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

    async def list(self, conversation_id: str) -> list[Task]:
        """Return tasks of one conversation."""
        ...

    async def next_pending(self) -> Task | None:
        """Return the oldest pending task, if any."""
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
