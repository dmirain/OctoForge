"""Durable delivery outbox for dialog results and broker notices."""

from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.agent.events import Failed, Finished, LoopEvent, TextDelta
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.tasks.api import Task, TaskNotFoundError, TaskStatus

from .runner_commands import Delivery
from .runner_constants import DEFAULT_TASK_ERROR
from .runner_text import delivery_started, stored_terminal

if TYPE_CHECKING:
    from .runner import ConversationRunner


class DeliveryOutbox:
    """Queues outcomes until a subscriber accepts their terminal event."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    def enqueue_redelivery(self, task: Task) -> None:
        if task.delivered_at is not None:
            return
        terminal = stored_terminal(task, DEFAULT_TASK_ERROR)
        if terminal is None:
            return
        self.enqueue_terminal(terminal, task)

    def enqueue_terminal(self, terminal: Finished | Failed, task: Task) -> None:
        events: tuple[LoopEvent, ...]
        if isinstance(terminal, Finished):
            message = replace(terminal.message, task_id=task.id)
            final = Finished(
                message=message,
                usage=terminal.usage,
                source_client_message_id=terminal.source_client_message_id,
            )
            events = (delivery_started(task), TextDelta(text=message.content), final)
        else:
            events = (delivery_started(task), Failed(error=terminal.error))
        self._runner._runtime.pending_deliveries.append(
            Delivery(events=events, task_id=task.id, exchange_id=task.exchange_id)
        )

    async def mark_streamed_delivered(self, task: Task) -> None:
        if task.delivered_at is not None:
            return
        if task.status in (TaskStatus.DONE, TaskStatus.FAILED):
            with suppress(TaskNotFoundError):
                await self._runner._stores.tasks.mark_delivered(task.id)

    async def deliver_notice(self, content: str, exchange_id: str | None = None) -> None:
        notice = ChatMessage(role=MessageRole.ASSISTANT, content=content, exchange_id=exchange_id)
        message_id = await self._runner._context.persist(notice)
        notice = replace(notice, id=message_id)
        self._runner._runtime.narrative.append(notice)
        delivery = Delivery(
            events=(TextDelta(text=content), Finished(message=notice)),
            task_id=None,
            exchange_id=exchange_id,
        )
        pending = self._runner._runtime.pending_deliveries
        pending.appendleft(delivery) if exchange_id is not None else pending.append(delivery)
        await self._runner._flush_deliveries()

    async def flush(self) -> None:
        runtime = self._runner._runtime
        async with runtime.flush_lock:
            while runtime.pending_deliveries and runtime.subscribers:
                delivery = runtime.pending_deliveries[0]
                accepted = 0
                for event in delivery.events:
                    accepted = self._runner._broadcast(event, delivery.exchange_id)
                if accepted == 0:
                    break
                if delivery.task_id is not None:
                    with suppress(TaskNotFoundError):
                        await self._runner._stores.tasks.mark_delivered(delivery.task_id)
                if runtime.pending_deliveries and runtime.pending_deliveries[0] is delivery:
                    runtime.pending_deliveries.popleft()
                else:
                    with suppress(ValueError):
                        runtime.pending_deliveries.remove(delivery)
