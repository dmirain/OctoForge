"""Registry of skills available to the agent."""

from octoforge_core.skills.base import Skill, SkillOrigin, SkillSpec
from octoforge_core.skills.errors import DuplicateSkillError, SkillNotFoundError


class SkillRegistry:
    """Holds skills of all origins under unique names."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._origins: dict[str, SkillOrigin] = {}

    def register(self, skill: Skill, origin: SkillOrigin) -> None:
        """Add a skill; names must be unique across origins."""
        name = skill.spec.name
        if name in self._skills:
            raise DuplicateSkillError(name)
        self._skills[name] = skill
        self._origins[name] = origin

    def get(self, name: str) -> Skill:
        """Return the skill by name."""
        try:
            return self._skills[name]
        except KeyError as exc:
            raise SkillNotFoundError(name) from exc

    def origin_of(self, name: str) -> SkillOrigin:
        """Return the origin of a registered skill."""
        try:
            return self._origins[name]
        except KeyError as exc:
            raise SkillNotFoundError(name) from exc

    def specs(self) -> list[SkillSpec]:
        """Return LLM-facing specs of all registered skills."""
        return [skill.spec for skill in self._skills.values()]
