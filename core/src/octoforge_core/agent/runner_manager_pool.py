"""Memoized construction and surface attachment of dialog actors."""

import asyncio
import logging
from typing import TYPE_CHECKING

from .runner_facade import ConversationRunner
from .runner_state import RunnerParts, RunnerSeed, RunnerStores

if TYPE_CHECKING:
    from .runner_manager import ConversationManager

logger = logging.getLogger(__name__)


class RunnerPool:
    """Shares one asynchronous build among concurrent callers of a dialog."""

    def __init__(self, manager: "ConversationManager") -> None:
        self._manager = manager

    async def get_or_create(self, user_id: str, channel: str) -> ConversationRunner:
        state = self._manager._state
        key = (user_id, channel)
        async with state.lock:
            build = state.builds.get(key)
            ours = build is None
            if build is None:
                build = asyncio.create_task(self._build(user_id, channel))
                state.builds[key] = build
        try:
            runner = await asyncio.shield(build)
        except BaseException:
            failed = build.done() and (build.cancelled() or build.exception() is not None)
            if failed:
                async with state.lock:
                    if state.builds.get(key) is build:
                        del state.builds[key]
            raise
        if ours:
            await self.attach(runner)
        return runner

    async def _build(self, user_id: str, channel: str) -> ConversationRunner:
        state = self._manager._state
        stores = state.stores
        async with stores.uow():
            dialog = await stores.dialogs.get_or_create(user_id, channel)
            claim = await stores.claims.claim(dialog.id, state.ownership.node_id)
        history = await stores.messages.list_hot_slice(dialog.id)
        runner = ConversationRunner(
            RunnerParts(
                RunnerSeed(dialog, history, claim),
                state.config,
                RunnerStores(
                    stores.messages,
                    stores.tasks,
                    stores.exchanges,
                    stores.claims,
                    stores.uow,
                ),
            )
        )
        runner.start()
        state.runners[dialog.id] = runner
        await self._manager._recovery.recover_dialog(runner)
        return runner

    async def attach(self, runner: ConversationRunner) -> None:
        surface = self._manager._state.surface
        if surface is None:
            return
        try:
            await surface.attach(runner)
        except Exception:
            logger.exception("surface attach failed: dialog=%s", runner.dialog_id)

    async def detach(self, runner: ConversationRunner) -> None:
        surface = self._manager._state.surface
        if surface is None:
            return
        try:
            await surface.detach(runner)
        except Exception:
            logger.exception("surface detach failed: dialog=%s", runner.dialog_id)

    @staticmethod
    def finished(build: asyncio.Task[ConversationRunner]) -> ConversationRunner | None:
        if not build.done() or build.cancelled() or build.exception() is not None:
            return None
        return build.result()
