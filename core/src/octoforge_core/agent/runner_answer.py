"""Starting, parking and cancelling answer obligations."""

import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from octoforge_core.agent.events import ProcessStarted
from octoforge_core.dialogs.api import ExchangeNotFoundError, ExchangeStatus
from octoforge_core.tasks.api import TaskKind

from .runner_constants import ANSWER_NOTE_KEY, TARIFF_LIMIT_NOTICE_TEMPLATE
from .runner_process import AnswerRequest, AnswerSource, ProcessTaskDraft

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class AnswerRuns:
    """Creates the sole process that owes an exchange its next answer."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def start(self, request: AnswerRequest) -> None:
        if not await self._runner._lifecycle.still_owns_dialog():
            self._runner._runtime.preempted = True
            logger.info(
                "refusing to answer, dialog moved: dialog=%s exchange=%s",
                self._runner.dialog_id,
                request.exchange.id,
            )
            return
        budget = await self._runner._usage.run_budget_verdict()
        if budget is not None:
            if request.notify_limit:
                await self._runner._tariffs.once(
                    ANSWER_NOTE_KEY,
                    TARIFF_LIMIT_NOTICE_TEMPLATE.format(
                        reason=budget.reason, used=budget.used, limit=budget.limit
                    ),
                )
            return
        draft = self._task_draft(request)
        async with self._runner._stores.uow():
            task = await self._runner._process_registry.prepare_task(draft)
            await self._runner._stores.exchanges.set_status(
                request.exchange.id, ExchangeStatus.IN_PROGRESS
            )
        narrative, watermark = await self._runner._context.assemble(request.exchange.id)
        process = self._runner._process_registry.create(
            task,
            [self._runner._context.system_message(), *narrative],
            narrative_built=True,
        )
        process.synced_len = len(process.branch)
        process.watermark = watermark
        if (
            request.cancel_epoch is not None
            and request.cancel_epoch < self._runner._runtime.cancel_epoch
        ):
            process.control.cancel()
        self._runner._broadcast(
            ProcessStarted(
                process_id=process.id,
                title=process.title,
                source_client_message_id=process.source_client_message_id,
            ),
            request.exchange.id,
        )

    @staticmethod
    def _task_draft(request: AnswerRequest) -> ProcessTaskDraft:
        source = AnswerSource(
            request.message.id,
            request.client_key,
            request.exchange.id,
        )
        return ProcessTaskDraft(
            request.exchange.title,
            request.message.content,
            TaskKind.ANSWER,
            source=source,
        )

    async def ask_user(self, process_id: str, question: str) -> bool:
        process = self._runner._runtime.processes.get(process_id)
        if process is None or process.exchange_id is None:
            return False
        process.asked = True
        await self._runner._deliver_notice(question, process.exchange_id)
        with suppress(ExchangeNotFoundError):
            await self._runner._stores.exchanges.set_status(
                process.exchange_id,
                ExchangeStatus.AWAITING_USER,
                pending_question=question,
            )
        return True

    async def cancel_parked(self) -> None:
        for exchange in await self._runner._stores.exchanges.list_live(self._runner.dialog_id):
            parked = exchange.status is ExchangeStatus.AWAITING_USER
            if parked:
                parked = self._runner._live_process_for(exchange.id) is None
            if parked or exchange.status is ExchangeStatus.COLLECTING:
                await self._runner._stores.exchanges.set_status(
                    exchange.id, ExchangeStatus.CANCELLED
                )

    def cancel_live(self) -> None:
        self._runner._runtime.cancel_epoch += 1
        for process in self._runner._runtime.processes.values():
            if process.exchange_id is not None:
                process.control.cancel()
