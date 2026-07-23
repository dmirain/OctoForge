"""Tool abstraction shared by all code tools."""

from dataclasses import dataclass
from typing import Any, Protocol

from octoforge_core.tasks.spawner import TaskDeleter, TaskSpawner


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
