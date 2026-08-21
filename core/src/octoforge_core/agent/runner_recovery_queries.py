"""Failure-isolated persistence queries used during actor recovery."""

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from octoforge_core.dialogs.api import ExchangeList
from octoforge_core.tasks.store import TaskList
from octoforge_core.time import utc_now

if TYPE_CHECKING:
    from .runner_manager import ConversationManager

logger = logging.getLogger(__name__)


class RecoveryQueries:
    def __init__(self, manager: "ConversationManager") -> None:
        self._manager = manager

    async def held_elsewhere(self, dialog_ids: frozenset[str]) -> frozenset[str]:
        if not dialog_ids:
            return frozenset()
        state = self._manager._state
        stale_before = utc_now() - timedelta(seconds=state.ownership.stale_after_seconds)
        try:
            return await state.stores.claims.held_elsewhere(
                dialog_ids, state.ownership.node_id, stale_before
            )
        except Exception:
            logger.exception("claim lookup failed; skipping recovery this start")
            return dialog_ids

    async def stranded_dialog_ids(self) -> list[str]:
        try:
            return await self._manager._state.stores.exchanges.list_stranded_dialog_ids()
        except Exception:
            logger.exception("stranded dialog sweep failed")
            return []

    async def tasks(self, dialog_id: str | None) -> tuple[TaskList, TaskList]:
        try:
            return await self._manager._state.stores.tasks.list_for_recovery(dialog_id)
        except Exception:
            logger.exception("task recovery sweep failed: dialog=%s", dialog_id)
            return [], []

    async def orphaned(self, dialog_id: str | None) -> TaskList:
        try:
            return await self._manager._state.stores.tasks.list_orphaned(dialog_id)
        except Exception:
            logger.exception("orphaned task sweep failed: dialog=%s", dialog_id)
            return []

    async def undelivered(self, dialog_id: str | None) -> TaskList:
        try:
            return await self._manager._state.stores.tasks.list_undelivered(dialog_id)
        except Exception:
            logger.exception("undelivered task sweep failed: dialog=%s", dialog_id)
            return []

    async def reopen(self, dialog_id: str) -> tuple[int, ExchangeList]:
        try:
            return await self._manager._state.stores.exchanges.reopen_and_list_stranded(dialog_id)
        except Exception:
            logger.exception("stranded exchange sweep failed: dialog=%s", dialog_id)
            return 0, []
