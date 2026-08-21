"""Serialized inbox and message-intake transaction of one dialog actor."""

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.agent.events import Failed
from octoforge_core.domain import ChatMessage, MessageKind, MessageRole, MessageSource

from .runner_api import DialogSubmission
from .runner_commands import (
    Command,
    Flush,
    ProcessTerminated,
    PromoteCollected,
    RouteApplication,
    Submit,
)
from .runner_constants import SUBMIT_FAILED_ERROR

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class ActorInbox:
    """Serializes routing, settlement and outbox drains for one narrative."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def submit(self, submission: DialogSubmission) -> None:
        source = submission.source or MessageSource()
        await self._runner._runtime.inbox.put(
            Submit(
                ChatMessage(
                    role=MessageRole.USER,
                    content=submission.content,
                    kind=source.kind,
                    attachments=source.attachments,
                ),
                client_message_id=submission.client_message_id,
                reply_to_exchange_id=submission.reply_to_exchange_id,
                cancel_epoch=self._runner._runtime.cancel_epoch,
                origin=source.origin,
            )
        )

    async def run(self) -> None:
        while True:
            command = await self._runner._runtime.inbox.get()
            try:
                await self._dispatch(command)
            except Exception:
                logger.exception(
                    "actor command failed: dialog=%s command=%s",
                    self._runner.dialog_id,
                    type(command).__name__,
                )
                if self._cancellation_pending():
                    raise asyncio.CancelledError from None
                if isinstance(command, Submit):
                    self._runner._broadcast(Failed(error=SUBMIT_FAILED_ERROR))
            if self._runner._runtime.preempted or self._cancellation_pending():
                raise asyncio.CancelledError from None

    async def _dispatch(self, command: Command) -> None:
        if isinstance(command, Submit):
            await self._handle_submit(command)
        elif isinstance(command, ProcessTerminated):
            await self._runner._settlement.handle(command)
        elif isinstance(command, Flush):
            await self._runner._flush_deliveries()
        elif isinstance(command, PromoteCollected):
            await self._runner._material_promotion.promote(command)

    async def _handle_submit(self, command: Submit) -> None:
        message = command.message
        try:
            if await self._is_duplicate(command.client_message_id):
                logger.info(
                    "duplicate submit skipped: dialog=%s key=%s",
                    self._runner.dialog_id,
                    command.client_message_id,
                )
                return
            message_id = await self._runner._context.persist(
                message, client_message_id=command.client_message_id
            )
        except Exception:
            logger.exception("submit persist failed: dialog=%s", self._runner.dialog_id)
            self._runner._broadcast(Failed(error=SUBMIT_FAILED_ERROR))
            return
        message = replace(message, id=message_id)
        await self._runner._usage.record_user_message()
        if message.kind is MessageKind.MATERIAL:
            await self._runner._material.collect(message, command)
            return
        live = await self._runner._stores.exchanges.list_live(self._runner.dialog_id)
        decision = await self._runner._routing.route(message, command, live)
        await self._runner._usage.record_routing(decision.usage)
        await self._runner._route_applier.apply(RouteApplication(message, decision, command, live))

    async def _is_duplicate(self, client_message_id: str | None) -> bool:
        if client_message_id is None:
            return False
        return await self._runner._stores.messages.find_by_client_id(
            self._runner.dialog_id, client_message_id
        )

    @staticmethod
    def _cancellation_pending() -> bool:
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0
