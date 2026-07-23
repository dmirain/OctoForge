"""Skill abstraction shared by all code tools."""

from dataclasses import dataclass
from typing import Any, Protocol

from octoforge_core.tasks.spawner import TaskDeleter, TaskSpawner


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """LLM-facing description of a skill."""

    name: str
    description: str
    parameters_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Per-invocation context available to skills.

    task_spawner/task_deleter are optional: contexts built outside the dialog
    actor (unit tests, one-off executions) simply have neither, and the task
    skills report that instead of failing to construct the context.
    owner_task_id is the background task the invocation belongs to (None for
    the foreground): it lets a skill recognize acting upon itself.
    """

    user_id: str
    channel: str
    dialog_id: str
    task_spawner: TaskSpawner | None = None
    task_deleter: TaskDeleter | None = None
    owner_task_id: str | None = None


class Skill(Protocol):
    """Executable unit the agent can invoke via tool calling."""

    @property
    def spec(self) -> SkillSpec:
        """LLM-facing description of the skill."""
        ...

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Run the skill with LLM-provided arguments and return text output."""
        ...
