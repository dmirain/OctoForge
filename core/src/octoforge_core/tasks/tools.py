"""Tools of the tasks module: spawn background tasks and list them."""

from typing import Any

from octoforge_core.ports import TaskStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError
from octoforge_core.tasks.models import Task

SPAWN_NAME = "task_spawn"
SPAWN_DESCRIPTION = (
    "Spawn a background task that will be solved asynchronously right now. "
    "Use it when the user asks to do work in the background (the result comes once, "
    "when it is ready). NOT for reminders or anything on a schedule — use cron_create."
)
SPAWN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short task title"},
        "prompt": {"type": "string", "description": "Full instruction for the background solver"},
    },
    "required": ["title", "prompt"],
}
NO_SPAWNER_MESSAGE = "task spawning is not available in this context"

LIST_NAME = "task_list"
LIST_DESCRIPTION = "List background tasks of this dialog with their statuses and results."
LIST_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
NO_TASKS_MESSAGE = "no tasks"


class TaskSpawnSkill:
    """Delegates task spawning to the TaskSpawner bound to the skill context."""

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SPAWN_NAME,
            description=SPAWN_DESCRIPTION,
            parameters_schema=SPAWN_SCHEMA,
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


class TaskListSkill:
    """Lists tasks of the current dialog."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=LIST_NAME,
            description=LIST_DESCRIPTION,
            parameters_schema=LIST_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        tasks = await self._store.list(context.dialog_id)
        if not tasks:
            return NO_TASKS_MESSAGE
        return "\n".join(self._format_task(task) for task in tasks)

    @staticmethod
    def _format_task(task: Task) -> str:
        line = f"{task.id} [{task.status.value}] {task.title}"
        if task.result is not None:
            line += f" -> {task.result}"
        if task.error is not None:
            line += f" -> error: {task.error}"
        return line
