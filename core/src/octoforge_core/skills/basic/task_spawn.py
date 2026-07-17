"""Basic skill that spawns a background task."""

from typing import Any

from octoforge_core.ports import TaskStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError
from octoforge_core.tasks.models import Task, TaskKind

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
SPAWNED_TEMPLATE = "task {task_id} spawned"


class TaskSpawnSkill:
    """Creates a PROMPT background task for the current dialog."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

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
        task = Task(
            dialog_id=context.dialog_id,
            user_id=context.user_id,
            channel=context.channel,
            title=title,
            kind=TaskKind.PROMPT,
            input={"prompt": prompt},
        )
        await self._store.add(task)
        return SPAWNED_TEMPLATE.format(task_id=task.id)
