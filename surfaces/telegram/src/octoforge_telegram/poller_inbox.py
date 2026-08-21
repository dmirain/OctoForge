"""Per-user ordered ingestion queues with album burst collection."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from octoforge_telegram.models import TelegramMessage
from octoforge_telegram.poller_types import Inbox

logger = logging.getLogger(__name__)
INBOX_IDLE_SECONDS = 60.0
INBOX_BACKLOG_WARNING = 50

BatchHandler = Callable[[str, list[TelegramMessage]], Awaitable[None]]


class InboxWorkers:
    def __init__(self, handler: BatchHandler, album_quiet_seconds: float) -> None:
        self._handler = handler
        self._album_quiet_seconds = album_quiet_seconds
        self.inboxes: dict[str, Inbox] = {}

    def enqueue(self, user_id: str, message: TelegramMessage) -> None:
        inbox = self.inboxes.setdefault(user_id, Inbox())
        inbox.pending.append(message)
        inbox.arrived.set()
        if len(inbox.pending) >= INBOX_BACKLOG_WARNING:
            logger.warning("Telegram ingestion backlog for %s: %d", user_id, len(inbox.pending))
        if inbox.worker is None or inbox.worker.done():
            inbox.worker = asyncio.create_task(self._work(user_id, inbox))
            inbox.worker.add_done_callback(report_worker_exit)

    def close(self) -> None:
        for inbox in tuple(self.inboxes.values()):
            if inbox.worker is not None:
                inbox.worker.cancel()

    async def _work(self, user_id: str, inbox: Inbox) -> None:
        while True:
            message = await self._next(inbox, INBOX_IDLE_SECONDS)
            if message is None:
                inbox.worker = None
                self.inboxes.pop(user_id, None)
                return
            inbox.busy = True
            try:
                batch = (
                    await self._collect_album(inbox, message)
                    if message.media_group_id is not None
                    else [message]
                )
                await self._handler(user_id, batch)
            finally:
                inbox.busy = False

    async def _next(self, inbox: Inbox, timeout: float) -> TelegramMessage | None:
        if not inbox.pending:
            inbox.arrived.clear()
            with suppress(TimeoutError):
                await asyncio.wait_for(inbox.arrived.wait(), timeout)
        return inbox.pending.popleft() if inbox.pending else None

    async def _collect_album(self, inbox: Inbox, first: TelegramMessage) -> list[TelegramMessage]:
        batch = [first]
        while True:
            message = await self._next(inbox, self._album_quiet_seconds)
            if message is None:
                return batch
            if message.media_group_id != first.media_group_id:
                inbox.pending.appendleft(message)
                return batch
            batch.append(message)


def report_worker_exit(worker: asyncio.Task[None]) -> None:
    if not worker.cancelled() and (error := worker.exception()) is not None:
        logger.error("Telegram ingestion worker crashed", exc_info=error)
