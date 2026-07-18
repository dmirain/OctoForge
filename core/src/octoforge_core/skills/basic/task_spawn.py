"""Basic skill that spawns a background task."""

from typing import Any

from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "task_spawn"
SKILL_DESCRIPTION = (
    "Spawn a background task that will be solved asynchronously. "
    "Use it when the user asks to do something in the background or later."
)
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short task title"},
        "prompt": {"type": "string", "description": "Full instruction for the background solver"},
    },
    "required": ["title", "prompt"],
}
NO_SPAWNER_MESSAGE = "task spawning is not available in this context"


class TaskSpawnSkill:
    """Delegates task spawning to the TaskSpawner bound to the skill context."""

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        title = arguments.get("title")
        if not isinstance(title, str) or not title:
            raise SkillArgumentsError("title must be a non-empty string")
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise SkillArgumentsError("prompt must be a non-empty string")
        if context.task_spawner is None:
            raise SkillArgumentsError(NO_SPAWNER_MESSAGE)
        return await context.task_spawner.spawn(title, prompt)
