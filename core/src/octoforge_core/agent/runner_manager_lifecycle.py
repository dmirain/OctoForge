"""Administrative eviction and orderly manager shutdown."""

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from .runner_facade import ConversationRunner

if TYPE_CHECKING:
    from .runner_manager import ConversationManager

logger = logging.getLogger(__name__)


class ManagerLifecycle:
    def __init__(self, manager: "ConversationManager") -> None:
        self._manager = manager

    async def evict(self, user_id: str, channel: str) -> None:
        state = self._manager._state
        async with state.lock:
            build = state.builds.pop((user_id, channel), None)
        if build is None:
            return
        if not build.done():
            build.cancel()
        runner = None
        with suppress(asyncio.CancelledError, Exception):
            runner = await build
        if runner is None:
            return
        state.runners.pop(runner.dialog_id, None)
        await self._manager._pool.detach(runner)
        try:
            await runner.stop()
        except Exception:
            logger.exception("runner stop failed: dialog=%s", runner.dialog_id)
        await self._release(runner)

    async def stop_all(self) -> None:
        state = self._manager._state
        await self._stop_heartbeat()
        async with state.lock:
            builds = tuple(state.builds.values())
            state.builds.clear()
            runners = tuple(state.runners.values())
            state.runners.clear()
        for build in builds:
            if not build.done():
                build.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await build
        for runner in runners:
            await self._manager._pool.detach(runner)
            try:
                await runner.stop()
            except Exception:
                logger.exception("runner stop failed: dialog=%s", runner.dialog_id)
            await self._release(runner)

    async def _stop_heartbeat(self) -> None:
        state = self._manager._state
        if state.heartbeat_task is None:
            return
        state.heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await state.heartbeat_task
        state.heartbeat_task = None

    async def _release(self, runner: ConversationRunner) -> None:
        claim = runner.claim
        try:
            await self._manager._state.stores.claims.release(
                claim.dialog_id, claim.owner, claim.generation
            )
        except Exception:
            logger.exception("claim release failed: dialog=%s", claim.dialog_id)
