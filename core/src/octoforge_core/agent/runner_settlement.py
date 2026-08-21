"""Exchange settlement and task outcome delivery after a process terminates."""

import logging
from typing import TYPE_CHECKING

from octoforge_core.dialogs.api import ExchangeSettlement, ExchangeStatus
from octoforge_core.tasks.api import TaskNotFoundError

from .runner_commands import ProcessTerminated, Unseen
from .runner_text import silent_done

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class ProcessSettlement:
    """Moves an obligation and its task delivery to their next durable states."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def handle(self, command: ProcessTerminated) -> None:
        await self.settle_exchange(command)
        task = command.task
        if task is None:
            try:
                task = await self._runner._stores.tasks.get(command.task_id)
            except TaskNotFoundError:
                return
        if silent_done(task):
            await self._runner._outbox.mark_streamed_delivered(task)
        elif command.terminal is None:
            self._runner._outbox.enqueue_redelivery(task)
        elif command.exchange_id is not None and command.delivered_live:
            await self._runner._outbox.mark_streamed_delivered(task)
        else:
            self._runner._outbox.enqueue_terminal(command.terminal, task)
        await self._runner._flush_deliveries()
        await self._runner._exchanges.sweep_unowned_open()

    async def settle_exchange(self, command: ProcessTerminated) -> None:
        if command.exchange_id is None or command.exchange_status is None:
            return
        cancelled = command.exchange_status is ExchangeStatus.CANCELLED
        if not cancelled and command.unseen_messages is Unseen.SPOKEN:
            if await self._settle_to(command, ExchangeStatus.OPEN):
                await self._runner._exchanges.resume_open(command.exchange_id)
            return
        if not cancelled and command.unseen_messages is Unseen.MATERIAL_ONLY:
            if await self._settle_to(command, ExchangeStatus.COLLECTING):
                await self._runner._stores.exchanges.touch(command.exchange_id)
            return
        await self._settle_to(
            command,
            command.exchange_status,
            keep_if_awaiting=not cancelled,
        )

    async def _settle_to(
        self,
        command: ProcessTerminated,
        status: ExchangeStatus,
        *,
        keep_if_awaiting: bool = False,
    ) -> bool:
        assert command.exchange_id is not None
        settled = await self._runner._stores.exchanges.settle_owned(
            ExchangeSettlement(
                command.exchange_id,
                command.task_id,
                status,
                keep_if_awaiting,
            )
        )
        logger.info(
            "settling exchange: dialog=%s exchange=%s to=%s unseen=%s applied=%s",
            self._runner.dialog_id,
            command.exchange_id,
            status.value,
            command.unseen_messages,
            settled is not None,
        )
        return settled is not None
