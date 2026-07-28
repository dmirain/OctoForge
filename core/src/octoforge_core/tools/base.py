"""Tool abstraction shared by all code tools.

Also home of the dialog-bound background-task ports (`TaskSpawner`,
`TaskDeleter`): they are capabilities of the actor exposed to tools through
`ToolContext`, i.e. tool-framework vocabulary. Keeping them here points the
dependency arrow the right way — domain modules (tasks, agent) import the
framework, never the reverse (2026-07-27 boundary audit, item 2).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class TaskSpawner(Protocol):
    """Creates a background task for the current dialog and starts its process."""

    async def spawn(self, title: str, prompt: str) -> str:
        """Spawn the task and return a confirmation text, or a refusal (e.g. limit)."""
        ...


class UserPrompter(Protocol):
    """Asks the user a question on behalf of the run that needs an answer."""

    async def ask(self, question: str) -> None:
        """Deliver the question and leave the run's exchange awaiting the user."""
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


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """LLM-facing description of a tool."""

    name: str
    description: str
    parameters_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-invocation context available to tools.

    task_spawner/task_deleter are optional: contexts built outside the dialog
    actor (unit tests, one-off executions) simply have neither, and the task
    tools report that instead of failing to construct the context.
    owner_task_id is the background task the invocation belongs to (None for
    the foreground): it lets a tool recognize acting upon itself.
    """

    user_id: str
    channel: str
    dialog_id: str
    task_spawner: TaskSpawner | None = None
    task_deleter: TaskDeleter | None = None
    user_prompter: UserPrompter | None = None
    owner_task_id: str | None = None


class Tool(Protocol):
    """Executable unit the agent can invoke via tool calling."""

    @property
    def spec(self) -> ToolSpec:
        """LLM-facing description of the tool."""
        ...

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Run the tool with LLM-provided arguments and return text output."""
        ...
