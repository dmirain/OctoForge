"""Ports for dialog-bound background task control."""

from enum import StrEnum
from typing import Protocol


class TaskSpawner(Protocol):
    """Creates a background task for the current dialog and starts its process."""

    async def spawn(self, title: str, prompt: str) -> str:
        """Spawn the task and return a confirmation text, or a refusal (e.g. limit)."""
        ...


class TaskDeleteOutcome(StrEnum):
    """Result of a dialog-bound task stop request."""

    # a live process was stopped; its finalization marks the row CANCELLED
    DELETED = "deleted"
    # no live process exists (terminal or orphaned row); the caller cancels it
    NOT_RUNNING = "not_running"


class TaskDeleter(Protocol):
    """Stops a live task process of the current dialog so it reaches a terminal state."""

    async def delete(self, task_id: str) -> TaskDeleteOutcome:
        """Stop the task's process; the finalization that follows marks the row CANCELLED.

        Callers must not pass the id of the very task they run in (the pump
        cannot be awaited from within); `TaskDeleteTool` refuses that case
        via `ToolContext.owner_task_id`.
        """
        ...
