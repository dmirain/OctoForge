"""Atomic persistence of process terminal outcomes."""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.agent.events import Failed, Finished, LoopEvent
from octoforge_core.tasks.api import Task, TaskStatus

from .runner_interruption import salvage
from .runner_process import Process

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class ProcessFinalizer:
    """Commits narrative and task state together, then reports metering/outcome."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def finalize(self, process: Process, terminal: LoopEvent) -> tuple[TaskStatus, Task]:
        if isinstance(terminal, Finished):
            status, task = await self._finish(process, terminal)
        elif isinstance(terminal, Failed):
            task = await self._runner._stores.tasks.mark_failed(
                process.task_id,
                terminal.error,
                delivered=self._delivery_is_certain(process, terminal.error),
            )
            status = TaskStatus.FAILED
        else:
            async with self._runner._stores.uow():
                salvaged = await salvage(self._runner, process)
                task = await self._runner._stores.tasks.mark_cancelled(process.task_id)
            if salvaged is not None:
                self._runner._runtime.narrative.extend(salvaged)
            status = TaskStatus.CANCELLED
        await self._runner._usage.record_run(process, terminal)
        await self._report_outcome(task, status)
        return status, task

    async def _finish(self, process: Process, terminal: Finished) -> tuple[TaskStatus, Task]:
        content = terminal.message.content
        if not content.strip():
            logger.info(
                "process finished with an empty final: dialog=%s task=%s title=%r",
                self._runner.dialog_id,
                process.task_id,
                process.title,
            )
        delivered = self._delivery_is_certain(process, content)
        if not content.strip():
            task = await self._runner._stores.tasks.mark_done(
                process.task_id, content, delivered=delivered
            )
            return TaskStatus.DONE, task
        message = replace(
            terminal.message,
            task_id=process.task_id,
            exchange_id=process.exchange_id,
        )
        async with self._runner._stores.uow():
            await self._runner._context.persist(message, terminal.usage)
            task = await self._runner._stores.tasks.mark_done(
                process.task_id, content, delivered=delivered
            )
        self._runner._runtime.narrative.append(message)
        if not process.narrative_built:
            await self._runner._context.compact_after_run_final()
        return TaskStatus.DONE, task

    @staticmethod
    def _delivery_is_certain(process: Process, content: str) -> bool:
        return not content.strip() or (
            process.exchange_id is not None and process.terminal_accepted
        )

    async def _report_outcome(self, task: Task, status: TaskStatus) -> None:
        listener = self._runner._config.task_outcome_listener
        if listener is None or "cron_job_id" not in task.input:
            return
        try:
            await listener.report_outcome(task, status)
        except Exception:
            logger.exception(
                "task outcome report failed: dialog=%s task=%s", self._runner.dialog_id, task.id
            )
