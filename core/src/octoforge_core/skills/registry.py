"""Registry of skills available to the agent."""

from octoforge_core.skills.base import Skill, SkillSpec
from octoforge_core.skills.errors import DuplicateSkillError, SkillNotFoundError


class SkillRegistry:
    """Holds the registered skills under unique names."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Add a skill; names must be unique across the registry."""
        name = skill.spec.name
        if name in self._skills:
            raise DuplicateSkillError(name)
        self._skills[name] = skill

    def get(self, name: str) -> Skill:
        """Return the skill by name."""
        try:
            return self._skills[name]
        except KeyError as exc:
            raise SkillNotFoundError(name) from exc

    def specs(self) -> list[SkillSpec]:
        """Return LLM-facing specs of all registered skills."""
        return [skill.spec for skill in self._skills.values()]
