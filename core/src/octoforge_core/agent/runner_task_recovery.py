"""Restarting tasks orphaned by process loss."""

from contextlib import suppress
from typing import TYPE_CHECKING

from octoforge_core.agent.events import Failed
from octoforge_core.dialogs.api import ExchangeNotFoundError, ExchangeStatus
from octoforge_core.tasks.api import Task, TaskKind

from .runner_commands import Delivery
from .runner_constants import TARIFF_RESTART_ERROR_TEMPLATE

if TYPE_CHECKING:
    from .runner import ConversationRunner


class TaskRecovery:
    """Rebuilds process branches while keeping durable obligations recoverable."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def start_orphaned(self, task: Task) -> None:
        if task.kind is not TaskKind.ANSWER:
            if await self._admit(task):
                self._runner._process_registry.start_background(task)
            return
        exchange_id = task.exchange_id
        if exchange_id is not None and await self._awaits_user(exchange_id):
            await self._runner._stores.tasks.mark_done(task.id, "")
            return
        if not await self._admit(task):
            if exchange_id is not None:
                with suppress(ExchangeNotFoundError):
                    await self._runner._stores.exchanges.set_status(
                        exchange_id, ExchangeStatus.OPEN
                    )
            return
        narrative, watermark = await self._runner._context.assemble(exchange_id)
        process = self._runner._process_registry.create(
            task,
            [self._runner._context.system_message(), *narrative],
            narrative_built=True,
        )
        process.synced_len = len(process.branch)
        process.watermark = watermark
        if exchange_id is not None:
            with suppress(ExchangeNotFoundError):
                await self._runner._stores.exchanges.set_status(
                    exchange_id, ExchangeStatus.IN_PROGRESS
                )

    async def _admit(self, task: Task) -> bool:
        budget = await self._runner._usage.run_budget_verdict()
        if budget is None:
            return True
        error = TARIFF_RESTART_ERROR_TEMPLATE.format(
            reason=budget.reason, used=budget.used, limit=budget.limit
        )
        await self._runner._stores.tasks.mark_failed(task.id, error)
        self._runner._runtime.pending_deliveries.append(
            Delivery(events=(Failed(error=error),), task_id=task.id)
        )
        await self._runner._flush_deliveries()
        return False

    async def _awaits_user(self, exchange_id: str) -> bool:
        try:
            exchange = await self._runner._stores.exchanges.get(exchange_id)
        except ExchangeNotFoundError:
            return False
        return exchange.status is ExchangeStatus.AWAITING_USER
