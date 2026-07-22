"""Registry of skills available to the agent."""

from collections.abc import Callable

from octoforge_core.skills.base import Skill, SkillContext, SkillSpec
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

    def specs(self, context: SkillContext | None = None) -> list[SkillSpec]:
        """Return LLM-facing specs of the skills visible in the given context.

        A skill may opt into context-dependent visibility by defining a
        `visible_to(context) -> bool` attribute (duck-typed opt-in: the Skill
        protocol is unchanged and most skills stay visible always). Without a
        context every skill is listed, exactly as before.
        """
        if context is None:
            return [skill.spec for skill in self._skills.values()]
        return [skill.spec for skill in self._skills.values() if _is_visible(skill, context)]


def _is_visible(skill: Skill, context: SkillContext) -> bool:
    visible_to: Callable[[SkillContext], bool] | None = getattr(skill, "visible_to", None)
    return visible_to is None or visible_to(context)
