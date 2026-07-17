"""External control channel for a running agent loop."""

import asyncio

from octoforge_core.domain import ChatMessage


class LoopControl:
    """Mailbox for injected messages plus a cancellation flag."""

    def __init__(self) -> None:
        self._mailbox: asyncio.Queue[ChatMessage] = asyncio.Queue()
        self._cancelled = asyncio.Event()

    def inject(self, message: ChatMessage) -> None:
        """Queue a message to be added to history at the next safe point."""
        self._mailbox.put_nowait(message)

    def cancel(self) -> None:
        """Request cancellation of the run."""
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def drain(self) -> list[ChatMessage]:
        """Take all queued injected messages."""
        drained: list[ChatMessage] = []
        while True:
            try:
                drained.append(self._mailbox.get_nowait())
            except asyncio.QueueEmpty:
                return drained
