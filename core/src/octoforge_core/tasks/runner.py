"""Background task runner."""

import asyncio
from collections.abc import Awaitable, Callable

from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.ports import LLMClient, TaskStore
from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.models import Task, TaskKind

OnTaskDone = Callable[[Task], Awaitable[None]]

POLL_INTERVAL_SECONDS = 0.2


class TaskRunner:
    """Executes pending tasks and reports completion."""

    def __init__(
        self,
        store: TaskStore,
        llm_client: LLMClient,
        registry: SkillRegistry,
        on_task_done: OnTaskDone,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._llm = llm_client
        self._registry = registry
        self._on_task_done = on_task_done
        self._poll_interval = poll_interval_seconds

    async def run_forever(self) -> None:
        """Poll and execute tasks until cancelled."""
        while True:
            task = await self._store.next_pending()
            if task is None:
                await asyncio.sleep(self._poll_interval)
                continue
            await self._execute(task)

    async def _execute(self, task: Task) -> None:
        await self._store.mark_running(task)
        try:
            result = await self._perform(task)
        except Exception as exc:  # task failures are recorded, not raised
            await self._store.mark_failed(task, str(exc))
        else:
            await self._store.mark_done(task, result)
        await self._on_task_done(task)

    async def _perform(self, task: Task) -> str:
        if task.kind is TaskKind.PROMPT:
            prompt = str(task.input["prompt"])
            reply = await self._llm.complete([ChatMessage(role=MessageRole.USER, content=prompt)])
            return reply.content
        skill_name = str(task.input["skill"])
        skill = self._registry.get(skill_name)
        arguments = task.input.get("params", {})
        context = SkillContext(conversation_id=task.conversation_id)
        return await skill.execute(arguments, context)
