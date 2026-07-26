"""External control channel for a running agent loop."""

import asyncio


class LoopControl:
    """Cancellation flag of one loop run.

    The loop used to also accept injected messages through this channel; the
    pull model replaced that: the process branch re-syncs from the dialog
    narrative at every iteration boundary (see `agent/runner.py`), so the
    only external signal left is cancellation.
    """

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()

    def cancel(self) -> None:
        """Request cancellation of the run."""
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    async def wait_cancelled(self) -> None:
        """Block until cancellation is requested.

        The loop races this against the next stream event, so a cancel
        interrupts a silent LLM stream immediately instead of waiting for
        the next token (or the idle timeout) to notice the flag.
        """
        await self._cancelled.wait()
