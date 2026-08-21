"""Ownership and revival rules for dialog obligations."""

import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from octoforge_core.dialogs.api import ExchangeNotFoundError, ExchangeStatus
from octoforge_core.domain import ChatMessage, MessageRole

from .runner_constants import PROCESS_LIMIT_NOTICE_TEMPLATE
from .runner_process import AnswerRequest, OwnerRequest, Process

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class ExchangeCoordinator:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    def live_process_for(self, exchange_id: str | None) -> Process | None:
        if exchange_id is None:
            return None
        return next(
            (
                process
                for process in self._runner._runtime.processes.values()
                if process.exchange_id == exchange_id
            ),
            None,
        )

    async def cancel(self, exchange_ids: tuple[str, ...]) -> set[str]:
        cancelled: set[str] = set()
        for exchange_id in exchange_ids:
            with suppress(ExchangeNotFoundError):
                answering = self.live_process_for(exchange_id)
                if answering is not None:
                    self._runner._process_registry.cancel(answering.task_id)
                await self._runner._stores.exchanges.set_status(
                    exchange_id, ExchangeStatus.CANCELLED
                )
                cancelled.add(exchange_id)
        return cancelled

    def cancelled_tasks(self, cancelled: set[str]) -> set[str]:
        live = {
            process.exchange_id
            for process in self._runner._runtime.processes.values()
            if process.exchange_id is not None
        }
        return cancelled & live

    async def ensure_owner(self, request: OwnerRequest) -> None:
        with suppress(ExchangeNotFoundError):
            async with self._runner._runtime.spawn_lock:
                if self.live_process_for(request.exchange_id) is not None:
                    return
                exchange = request.known
                if exchange is None:
                    exchange = await self._runner._stores.exchanges.get(request.exchange_id)
                if self._runner._process_registry.exceeds_limit(
                    self.cancelled_tasks(set(request.cancelled))
                ):
                    await self.reject_for_limit(request.message)
                    return
                await self._runner._answer.start(request.to_answer_request(exchange))

    async def resume_open(self, exchange_id: str, *, notify_limit: bool = True) -> None:
        message = next(
            (
                item
                for item in reversed(self._runner._runtime.narrative)
                if item.role is MessageRole.USER and item.exchange_id == exchange_id
            ),
            None,
        )
        if message is None:
            logger.warning(
                "cannot resume exchange, its message left the hot tail: dialog=%s exchange=%s",
                self._runner.dialog_id,
                exchange_id,
            )
            return
        with suppress(ExchangeNotFoundError):
            async with self._runner._runtime.spawn_lock:
                exchange = await self._runner._stores.exchanges.get(exchange_id)
                if exchange.status is not ExchangeStatus.OPEN:
                    return
                if self.live_process_for(exchange_id) is not None:
                    return
                if self._runner._process_registry.exceeds_limit(set()):
                    if notify_limit:
                        await self.reject_for_limit(message)
                    return
                await self._runner._answer.start(
                    AnswerRequest(exchange, message, notify_limit=notify_limit)
                )

    async def sweep_unowned_open(self) -> None:
        try:
            stranded = await self._runner._stores.exchanges.list_unowned_open(
                self._runner.dialog_id
            )
        except Exception:
            logger.exception("unowned-open sweep failed: dialog=%s", self._runner.dialog_id)
            return
        for exchange in stranded:
            await self.resume_open(exchange.id, notify_limit=False)

    async def reject_for_limit(self, message: ChatMessage) -> None:
        notice = PROCESS_LIMIT_NOTICE_TEMPLATE.format(
            message=message.content,
            limit=self._runner._config.max_processes,
            titles=self._runner._process_registry.active_titles(),
        )
        await self._runner._deliver_notice(notice)
