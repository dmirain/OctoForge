"""Admission and lifecycle of background RUN processes."""

from typing import TYPE_CHECKING

from octoforge_core.agent.events import Failed
from octoforge_core.tasks.api import Task, TaskKind
from octoforge_core.tools.base import TaskDeleteOutcome

from .runner_commands import Delivery
from .runner_constants import (
    CRON_LIMIT_NOTICE_TEMPLATE,
    RESTART_LIMIT_ERROR,
    SPAWN_REFUSAL_TEMPLATE,
    SPAWNED_TEMPLATE,
    TARIFF_SPAWN_REFUSAL_TEMPLATE,
)
from .runner_process import ProcessTaskDraft

if TYPE_CHECKING:
    from .runner import ConversationRunner


class BackgroundJobs:
    """Starts bounded tool and cron work without coupling it to answer runs."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def spawn(self, title: str, prompt: str) -> str:
        budget = await self._runner._usage.run_budget_verdict()
        if budget is not None:
            return TARIFF_SPAWN_REFUSAL_TEMPLATE.format(
                reason=budget.reason, used=budget.used, limit=budget.limit
            )
        async with self._runner._runtime.spawn_lock:
            if self._runner._process_registry.exceeds_limit(set()):
                return self._spawn_refusal()
            task = await self._runner._process_registry.prepare_task(
                ProcessTaskDraft(title, prompt, TaskKind.RUN)
            )
            self._runner._process_registry.start_background(task)
        return SPAWNED_TEMPLATE.format(task_id=task.id)

    async def wake(self, title: str, prompt: str, cron_job_id: str) -> bool:
        budget = await self._runner._usage.run_budget_verdict()
        if budget is not None:
            await self._runner._tariffs.publish_cron(title, cron_job_id, budget)
            return False
        async with self._runner._runtime.spawn_lock:
            over_limit = self._runner._process_registry.exceeds_limit(set())
            if not over_limit:
                draft = ProcessTaskDraft(title, prompt, TaskKind.RUN, cron_job_id)
                task = await self._runner._process_registry.prepare_task(draft)
                self._runner._process_registry.start_background(task)
        if over_limit:
            notice = CRON_LIMIT_NOTICE_TEMPLATE.format(
                title=title,
                limit=self._runner._config.max_processes,
                titles=self._runner._process_registry.active_titles(),
            )
            await self._runner._deliver_notice(notice)
        return not over_limit

    async def delete(self, task_id: str) -> TaskDeleteOutcome:
        process = self._runner._runtime.processes.get(task_id)
        if process is None:
            return TaskDeleteOutcome.NOT_RUNNING
        process.control.cancel()
        if process.pump is not None:
            await process.pump
        return TaskDeleteOutcome.DELETED

    async def restart(self, task: Task) -> None:
        async with self._runner._runtime.spawn_lock:
            if not self._runner._process_registry.exceeds_limit(set()):
                await self._runner._recovery.start_orphaned(task)
                return
        await self._runner._stores.tasks.mark_failed(task.id, RESTART_LIMIT_ERROR)
        self._runner._runtime.pending_deliveries.append(
            Delivery(events=(Failed(error=RESTART_LIMIT_ERROR),), task_id=task.id)
        )
        await self._runner._flush_deliveries()

    def _spawn_refusal(self) -> str:
        return SPAWN_REFUSAL_TEMPLATE.format(
            limit=self._runner._config.max_processes,
            titles=self._runner._process_registry.active_titles(),
        )
