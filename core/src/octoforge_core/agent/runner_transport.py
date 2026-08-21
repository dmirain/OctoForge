"""Transport-facing event stream and dialog identity of an actor."""

import asyncio

from octoforge_core.agent.events import LoopEvent
from octoforge_core.dialogs.api import DialogClaim, MessageRepository
from octoforge_core.domain import ChatMessage

from .runner_api import SubscriberQueue
from .runner_broadcast import EventBroadcaster
from .runner_commands import Flush
from .runner_constants import STREAM_CLOSED, SUBSCRIBER_QUEUE_SIZE
from .runner_narrative import NarrativeContext
from .runner_outbox import DeliveryOutbox
from .runner_state import RunnerRuntime, RunnerSeed, RunnerStores


class RunnerTransport:
    """Exposes identity and subscriber delivery without process internals."""

    _seed: RunnerSeed
    _stores: RunnerStores
    _runtime: RunnerRuntime
    _broadcaster: EventBroadcaster
    _outbox: DeliveryOutbox
    _context: NarrativeContext

    def subscribe(self) -> SubscriberQueue:
        queue: SubscriberQueue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        if self._runtime.stood_down:
            queue.put_nowait(STREAM_CLOSED)
        else:
            self._runtime.subscribers.add(queue)
            self._runtime.inbox.put_nowait(Flush())
        return queue

    def unsubscribe(self, queue: SubscriberQueue) -> None:
        self._runtime.subscribers.discard(queue)

    def history(self) -> list[ChatMessage]:
        return list(self._runtime.narrative)

    @property
    def dialog_id(self) -> str:
        return self._seed.dialog.id

    @property
    def user_id(self) -> str:
        return self._seed.dialog.user_id

    @property
    def channel(self) -> str:
        return self._seed.dialog.channel

    @property
    def claim(self) -> DialogClaim:
        return self._seed.claim

    def _broadcast(self, event: LoopEvent, exchange_id: str | None = None) -> int:
        return self._broadcaster.broadcast(event, exchange_id)

    async def _flush_deliveries(self) -> None:
        await self._outbox.flush()

    async def _deliver_notice(self, content: str, exchange_id: str | None = None) -> None:
        await self._outbox.deliver_notice(content, exchange_id)

    @property
    def _actor_task(self) -> asyncio.Task[None] | None:
        return self._runtime.actor_task

    @property
    def _messages(self) -> MessageRepository:
        return self._stores.messages

    @_messages.setter
    def _messages(self, value: MessageRepository) -> None:
        self._stores.messages = value
