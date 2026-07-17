"""Basic skill that lists background tasks of the current dialog."""

from typing import Any

from octoforge_core.ports import TaskStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.tasks.models import Task

SKILL_NAME = "task_list"
SKILL_DESCRIPTION = "List background tasks of this dialog with their statuses and results."
PARAMETERS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
NO_TASKS_MESSAGE = "no tasks"


class TaskListSkill:
    """Lists tasks of the current dialog."""

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
