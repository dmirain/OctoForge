"""Periodic execution of the MCP mirror sweep."""

import asyncio
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class McpSweep(Protocol):
    async def sync_all(self) -> None: ...


class McpSyncLoop:
    """Run mirror refreshes forever without letting one sweep end the loop."""

    def __init__(self, sync: McpSweep, interval_seconds: float) -> None:
        self._sync = sync
        self._interval_seconds = interval_seconds

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self._sync.sync_all()
            except Exception:
                logger.exception("MCP sync sweep failed")
