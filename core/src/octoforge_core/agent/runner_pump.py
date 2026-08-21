"""Process pump lifecycle from stream through actor settlement."""

import logging
from typing import TYPE_CHECKING

from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    LoopEvent,
    ProcessCompleted,
)
from octoforge_core.agent.loop import format_error
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.domain import MessageKind, MessageRole
from octoforge_core.tasks.api import Task, TaskStatus

from .runner_commands import ProcessTerminated, Unseen
from .runner_process import Process, PumpOutcome

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class ProcessPump:
    """Always releases a process slot even if streaming or persistence fails."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def run(self, process: Process) -> None:
        status = TaskStatus.FAILED
        task: Task | None = None
        terminal: LoopEvent = Cancelled()
        try:
            try:
                terminal = await self._runner._stream.terminal(process)
            except Exception as exc:
                logger.exception(
                    "process stream setup failed: dialog=%s process=%s",
                    self._runner.dialog_id,
                    process.id,
                )
                terminal = self._runner._stream.fail(process, format_error(exc))
            try:
                status, task = await self._runner._finalizer.finalize(process, terminal)
            except Exception:
                logger.exception(
                    "process finalize failed: dialog=%s process=%s",
                    self._runner.dialog_id,
                    process.id,
                )
        finally:
            self._terminate(process, PumpOutcome(status, terminal, task))

    def _terminate(
        self,
        process: Process,
        outcome: PumpOutcome,
    ) -> None:
        logger.info(
            "process terminated: dialog=%s task=%s exchange=%s status=%s",
            self._runner.dialog_id,
            process.task_id,
            process.exchange_id,
            outcome.status.value,
        )
        self._runner._runtime.processes.pop(process.id, None)
        if self._runner._config.response_memory is not None:
            self._runner._config.response_memory.drop_scope(process.task_id)
        self._runner._broadcast(
            ProcessCompleted(process.id, process.title, outcome.status.value),
            process.exchange_id,
        )
        self._runner._runtime.inbox.put_nowait(
            ProcessTerminated(
                task_id=process.task_id,
                terminal=(
                    outcome.terminal if isinstance(outcome.terminal, (Finished, Failed)) else None
                ),
                exchange_id=process.exchange_id,
                delivered_live=process.terminal_accepted,
                exchange_status=self._exchange_outcome(outcome.status),
                unseen_messages=self._unseen_kind(process),
                task=outcome.task,
            )
        )

    def _unseen_kind(self, process: Process) -> Unseen:
        if not process.narrative_built or process.exchange_id is None:
            return Unseen.NONE
        arrived = [
            message
            for message in self._runner._runtime.narrative[process.watermark :]
            if message.role is MessageRole.USER and message.exchange_id == process.exchange_id
        ]
        if not arrived:
            return Unseen.NONE
        if all(message.kind is MessageKind.MATERIAL for message in arrived):
            return Unseen.MATERIAL_ONLY
        return Unseen.SPOKEN

    @staticmethod
    def _exchange_outcome(status: TaskStatus) -> ExchangeStatus:
        if status is TaskStatus.DONE:
            return ExchangeStatus.ANSWERED
        if status is TaskStatus.CANCELLED:
            return ExchangeStatus.CANCELLED
        return ExchangeStatus.FAILED
