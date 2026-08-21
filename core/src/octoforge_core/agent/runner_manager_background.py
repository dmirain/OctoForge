"""Ownership-safe routing of cron and collection sweeps."""

from typing import TYPE_CHECKING

from octoforge_core.cron.api import CronWake, WakeOutcome

from .runner_facade import ConversationRunner

if TYPE_CHECKING:
    from .runner_manager import ConversationManager


class ManagerBackground:
    """Acts locally, adopts unowned dialogs and leaves live peers alone."""

    def __init__(self, manager: "ConversationManager") -> None:
        self._manager = manager

    async def runner_for(self, user_id: str, channel: str) -> ConversationRunner | None:
        state = self._manager._state
        dialog = await state.stores.dialogs.get_or_create(user_id, channel)
        existing = state.runners.get(dialog.id)
        if existing is not None:
            return existing
        held = await self._manager._queries.held_elsewhere(frozenset({dialog.id}))
        if held:
            return None
        return await self._manager.get_or_create_runner(user_id, channel)

    async def promote(self, user_id: str, channel: str, exchange_id: str) -> None:
        runner = await self.runner_for(user_id, channel)
        if runner is not None:
            await runner.promote_collected(exchange_id)

    async def wake(self, request: CronWake) -> WakeOutcome:
        runner = await self.runner_for(request.user_id, request.channel)
        if runner is None:
            return WakeOutcome.NOT_OURS
        started = await runner.wake(request.title, request.prompt, request.cron_job_id)
        return WakeOutcome.DELIVERED if started else WakeOutcome.LIMITED
