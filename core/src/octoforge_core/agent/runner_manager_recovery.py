"""Adoption and recovery of dialogs left by a dead owner."""

import asyncio
import logging
from typing import TYPE_CHECKING

from .runner_facade import ConversationRunner

if TYPE_CHECKING:
    from .runner_manager import ConversationManager

logger = logging.getLogger(__name__)


class ManagerRecovery:
    """Treats startup and ownership handover as the same recovery operation."""

    def __init__(self, manager: "ConversationManager") -> None:
        self._manager = manager

    async def recover_dialog(self, runner: ConversationRunner) -> None:
        dialog_id = runner.dialog_id
        queries = self._manager._queries
        (reopened, stranded), (orphaned, undelivered) = await asyncio.gather(
            queries.reopen(dialog_id),
            queries.tasks(dialog_id),
        )
        for task in orphaned:
            try:
                await runner.restart_task(task)
            except Exception:
                logger.exception("orphaned task restart failed: task=%s", task.id)
        revived = 0
        if stranded:
            try:
                await runner.resume_stranded()
                revived = len(stranded)
            except Exception:
                logger.exception("stranded exchange revive failed: dialog=%s", dialog_id)
        for task in undelivered:
            runner.request_result_delivery(task.id)
        if reopened or revived:
            logger.info(
                "recovered on claim: dialog=%s reopened=%s revived=%s",
                dialog_id,
                reopened,
                revived,
            )

    async def recover_interrupted(self) -> None:
        queries = self._manager._queries
        orphaned, undelivered = await asyncio.gather(
            queries.orphaned(None), queries.undelivered(None)
        )
        candidates = frozenset(await queries.stranded_dialog_ids()).union(
            task.dialog_id for task in (*orphaned, *undelivered)
        )
        mine = candidates - await queries.held_elsewhere(candidates)
        recovered = sum([await self._adopt(dialog_id) for dialog_id in mine])
        logger.info(
            "startup recovery: dialogs=%s skipped=%s failed=%s",
            recovered,
            len(candidates) - len(mine),
            len(mine) - recovered,
        )

    async def _adopt(self, dialog_id: str) -> bool:
        try:
            dialog = await self._manager._state.stores.dialogs.get(dialog_id)
            await self._manager.get_or_create_runner(dialog.user_id, dialog.channel)
        except Exception:
            logger.exception("dialog recovery failed: dialog=%s", dialog_id)
            return False
        return True
