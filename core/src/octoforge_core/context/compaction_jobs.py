"""One-running-compaction-per-dialog lifecycle and reactive waiting."""

import asyncio
import logging
from contextlib import suppress

from octoforge_core.context.compaction_runner import CompactionRunner
from octoforge_core.context.ports import SummaryStore
from octoforge_core.domain import Dialog

logger = logging.getLogger(__name__)


class DialogCompactions:
    """Own background compaction tasks and serialize work per dialog."""

    def __init__(self, store: SummaryStore, runner: CompactionRunner) -> None:
        self._store = store
        self._runner = runner
        self._running: dict[str, asyncio.Task[None]] = {}

    def trigger(self, dialog: Dialog) -> None:
        self._start(dialog)

    async def close(self, dialog_id: str) -> None:
        task = self._running.pop(dialog_id, None)
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def compact_now(self, dialog: Dialog) -> bool:
        before = await self._store.max_seq_to(dialog.id)
        running = self._running.get(dialog.id)
        if running is not None and not running.done():
            with suppress(asyncio.CancelledError):
                await running
            if await self._store.max_seq_to(dialog.id) > before:
                return True
        task = self._start(dialog)
        try:
            await task
        except Exception:
            logger.warning("synchronous compaction failed for %s", dialog.id, exc_info=True)
            return False
        return await self._store.max_seq_to(dialog.id) > before

    def _start(self, dialog: Dialog) -> asyncio.Task[None]:
        running = self._running.get(dialog.id)
        if running is not None and not running.done():
            return running
        task = asyncio.create_task(self._runner.run(dialog))
        self._running[dialog.id] = task
        task.add_done_callback(lambda done: self._on_done(dialog.id, done))
        return task

    def _on_done(self, dialog_id: str, task: asyncio.Task[None]) -> None:
        if self._running.get(dialog_id) is task:
            self._running.pop(dialog_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning("context compaction failed: dialog=%s", dialog_id, exc_info=error)
