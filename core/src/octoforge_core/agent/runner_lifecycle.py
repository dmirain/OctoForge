"""Actor startup, shutdown and claim-loss lifecycle."""

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class RunnerLifecycle:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    def start(self) -> None:
        runtime = self._runner._runtime
        if runtime.actor_task is None:
            runtime.actor_task = asyncio.create_task(self._runner._actor.run())
            runtime.actor_task.add_done_callback(self._on_actor_done)

    def _on_actor_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "actor task exited unexpectedly: dialog=%s",
                self._runner.dialog_id,
                exc_info=error,
            )

    async def stop(self) -> None:
        runtime = self._runner._runtime
        for process in runtime.processes.values():
            process.control.cancel()
        pending: list[asyncio.Task[None]] = []
        for process in runtime.processes.values():
            if process.pump is not None:
                process.pump.cancel()
                pending.append(process.pump)
        if runtime.actor_task is not None:
            runtime.actor_task.cancel()
            pending.append(runtime.actor_task)
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
        await self._runner._config.compactor.aclose(self._runner.dialog_id)

    async def stand_down(self) -> None:
        runtime = self._runner._runtime
        if runtime.stood_down:
            return
        runtime.stood_down = True
        logger.info(
            "standing down: dialog=%s owner=%s generation=%s",
            self._runner.dialog_id,
            self._runner.claim.owner,
            self._runner.claim.generation,
        )
        for queue in tuple(runtime.subscribers):
            self._runner._broadcaster.close_stream(queue)
        runtime.subscribers.clear()
        await self.stop()

    async def still_owns_dialog(self) -> bool:
        try:
            generation = await self._runner._stores.claims.current_generation(
                self._runner.dialog_id
            )
        except Exception:
            logger.exception("ownership check failed: dialog=%s", self._runner.dialog_id)
            return True
        return generation is None or generation == self._runner.claim.generation
