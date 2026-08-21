"""Claim heartbeat and preemption of moved dialogs."""

import asyncio
import logging
from typing import TYPE_CHECKING

from octoforge_core.dialogs.api import DialogClaimList

from .runner_facade import ConversationRunner

if TYPE_CHECKING:
    from .runner_manager import ConversationManager

logger = logging.getLogger(__name__)


class OwnershipHeartbeat:
    def __init__(self, manager: "ConversationManager") -> None:
        self._manager = manager

    def start(self) -> None:
        state = self._manager._state
        if state.heartbeat_task is None:
            state.heartbeat_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._manager._state.ownership.heartbeat_seconds)
            try:
                await self.beat_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("claim heartbeat failed")

    async def beat_once(self) -> None:
        runners = tuple(self._manager._state.runners.values())
        if not runners:
            return
        claims: DialogClaimList = [runner.claim for runner in runners]
        kept = await self._manager._state.stores.claims.heartbeat(claims)
        for runner in runners:
            if runner.dialog_id not in kept:
                await self._drop_preempted(runner)

    async def _drop_preempted(self, runner: ConversationRunner) -> None:
        state = self._manager._state
        async with state.lock:
            for key, build in tuple(state.builds.items()):
                if self._manager._pool.finished(build) is runner:
                    del state.builds[key]
            state.runners.pop(runner.dialog_id, None)
        await self._manager._pool.detach(runner)
        try:
            await runner.stand_down()
        except Exception:
            logger.exception("stand-down failed: dialog=%s", runner.dialog_id)
