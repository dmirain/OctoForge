"""Subscriber fan-out with lossless delivery of critical events."""

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    LoopEvent,
    ProcessCompleted,
    ProcessStarted,
)

from .runner_api import ConversationEvent, SubscriberQueue
from .runner_constants import STREAM_CLOSED

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)

CRITICAL_EVENTS = (Finished, Failed, Cancelled, ProcessStarted, ProcessCompleted)


class EventBroadcaster:
    """Fans events out while preserving terminals and stream closure."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    def broadcast(self, event: LoopEvent, exchange_id: str | None = None) -> int:
        runtime = self._runner._runtime
        runtime.seq += 1
        envelope = ConversationEvent(
            dialog_id=self._runner.dialog_id,
            seq=runtime.seq,
            payload=event,
            exchange_id=exchange_id,
        )
        critical = isinstance(event, CRITICAL_EVENTS)
        accepted = 0
        for queue in runtime.subscribers:
            try:
                queue.put_nowait(envelope)
                accepted += 1
            except asyncio.QueueFull:
                if critical and self._evict_and_put(queue, envelope):
                    accepted += 1
                runtime.dropped_events += 1
                logger.debug(
                    "dropped SSE event: dialog=%s seq=%s dropped_total=%s",
                    self._runner.dialog_id,
                    runtime.seq,
                    runtime.dropped_events,
                )
        return accepted

    @staticmethod
    def close_stream(queue: SubscriberQueue) -> None:
        while True:
            try:
                queue.put_nowait(STREAM_CLOSED)
                return
            except asyncio.QueueFull:
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()

    @staticmethod
    def _evict_and_put(queue: SubscriberQueue, envelope: ConversationEvent) -> bool:
        drained: list[ConversationEvent | None] = []
        while True:
            try:
                drained.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        victim = next(
            (
                index
                for index, item in enumerate(drained)
                if item is not None and not isinstance(item.payload, CRITICAL_EVENTS)
            ),
            None,
        )
        if victim is not None:
            del drained[victim]
            drained.append(envelope)
        for item in drained:
            with suppress(asyncio.QueueFull):
                queue.put_nowait(item)
        return victim is not None
