"""Skill abstraction shared by basic and dynamic skills."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from octoforge_core.tasks.spawner import TaskSpawner


class SkillOrigin(StrEnum):
    """Where a skill implementation comes from."""

    BASIC = "basic"
    DYNAMIC = "dynamic"


@dataclass(frozen=True, slots=True)
class SkillSpec:
    """LLM-facing description of a skill."""

    name: str
    description: str
    parameters_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SkillContext:
    """Per-invocation context available to skills.

    task_spawner is optional: contexts built outside the dialog actor (unit
    tests, one-off executions) simply have no spawner, and task_spawn reports
    that instead of failing to construct the context.
    """

    user_id: str
    channel: str
    dialog_id: str
    task_spawner: TaskSpawner | None = None


class Skill(Protocol):
    """Executable unit the agent can invoke via tool calling."""

    @property
    def spec(self) -> SkillSpec:
        """LLM-facing description of the skill."""
        ...

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Run the skill with LLM-provided arguments and return text output."""
        ...
