"""Creation and in-memory registry of task-backed agent processes."""

import asyncio
from typing import TYPE_CHECKING, Any

from octoforge_core.agent.control import LoopControl
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.tasks.api import Task, TaskKind, TaskStatus
from octoforge_core.time import utc_now

from .runner_constants import BACKGROUND_TASK_PROMPT
from .runner_process import (
    Process,
    ProcessTaskDraft,
    task_client_source,
    task_origin,
    task_source_message,
)
from .runner_text import with_date_envelope

if TYPE_CHECKING:
    from .runner import ConversationRunner


class ProcessRegistry:
    """Creates process state and owns the actor's bounded process slots."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def prepare_task(self, draft: ProcessTaskDraft) -> Task:
        task_input: dict[str, Any] = {"prompt": draft.prompt}
        if draft.kind is TaskKind.RUN:
            task_input["title"] = draft.title
        if draft.cron_job_id is not None:
            task_input["cron_job_id"] = draft.cron_job_id
            task_input["fired_at"] = utc_now().isoformat()
        if draft.kind is TaskKind.ANSWER:
            task_input["source_message_id"] = (
                draft.source.message_id if draft.source is not None else None
            )
            if draft.source is not None and draft.source.client_message_id is not None:
                task_input["source_client_message_id"] = draft.source.client_message_id
            if draft.source is not None and draft.source.exchange_id is not None:
                task_input["exchange_id"] = draft.source.exchange_id
        task = Task(
            dialog_id=self._runner.dialog_id,
            title=draft.title,
            kind=draft.kind,
            exchange_id=draft.source.exchange_id if draft.source is not None else None,
            input=task_input,
            status=TaskStatus.RUNNING,
            started_at=utc_now(),
        )
        await self._runner._stores.tasks.add(task)
        return task

    def start_background(self, task: Task) -> None:
        prompt = task.input.get("prompt")
        self.create(
            task,
            [
                ChatMessage(role=MessageRole.SYSTEM, content=BACKGROUND_TASK_PROMPT),
                with_date_envelope(
                    ChatMessage(
                        role=MessageRole.USER,
                        content=prompt if isinstance(prompt, str) else task.title,
                    )
                ),
            ],
            narrative_built=False,
        )

    def create(self, task: Task, branch: list[ChatMessage], *, narrative_built: bool) -> Process:
        process = Process(
            id=task.id,
            title=task.title,
            task_id=task.id,
            control=LoopControl(),
            branch=branch,
            narrative_built=narrative_built,
            source_message_id=task_source_message(task),
            source_client_message_id=task_client_source(task),
            exchange_id=task.exchange_id,
            origin=task_origin(task),
        )
        process.pump = asyncio.create_task(self._runner._pump.run(process))
        self._runner._runtime.processes[process.id] = process
        return process

    def cancel(self, target_id: str) -> bool:
        process = self._runner._runtime.processes.get(target_id)
        if process is None:
            return False
        process.control.cancel()
        return True

    def exceeds_limit(self, cancelled: set[str]) -> bool:
        return (
            len(self._runner._runtime.processes) - len(cancelled) + 1
            > self._runner._config.max_processes
        )

    def active_titles(self) -> str:
        return ", ".join(p.title for p in self._runner._runtime.processes.values())
