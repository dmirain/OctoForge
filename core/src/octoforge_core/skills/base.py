"""Skill abstraction shared by basic and dynamic skills."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


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
    """Per-invocation context available to skills."""

    conversation_id: str


class Skill(Protocol):
    """Executable unit the agent can invoke via tool calling."""

    @property
    def spec(self) -> SkillSpec:
        """LLM-facing description of the skill."""
        ...

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Run the skill with LLM-provided arguments and return text output."""
        ...
