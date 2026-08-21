"""Agent tool ports bound to one dialog actor."""

from typing import TYPE_CHECKING

from octoforge_core.tools.base import TaskDeleteOutcome

if TYPE_CHECKING:
    from .runner import ConversationRunner


class DialogUserPrompter:
    def __init__(self, runner: "ConversationRunner", process_id: str) -> None:
        self._runner = runner
        self._process_id = process_id

    async def ask(self, question: str) -> bool:
        return await self._runner.ask_user(self._process_id, question)


class DialogImageInspector:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def look(self, question: str) -> str:
        return await self._runner.look_at_image(question)


class DialogTaskSpawner:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def spawn(self, title: str, prompt: str) -> str:
        return await self._runner.spawn_task(title, prompt)


class DialogTaskDeleter:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def delete(self, task_id: str) -> TaskDeleteOutcome:
        return await self._runner.delete_task(task_id)
