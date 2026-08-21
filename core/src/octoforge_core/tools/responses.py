"""Framework port for task-scoped response cleanup."""

from typing import Protocol


class TaskScopedResponses(Protocol):
    """Response memory swept when a task process terminates."""

    def drop_scope(self, scope: str) -> None:
        """Forget every remembered response of one task."""
        ...
