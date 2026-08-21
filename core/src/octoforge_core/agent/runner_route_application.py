"""Applying a routing decision atomically to narrative and obligations."""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.agent.router import RouteAction
from octoforge_core.dialogs.api import Exchange, ExchangeStatus
from octoforge_core.time import utc_now

from .runner_commands import RouteApplication, RouteTarget
from .runner_constants import NUDGE_AFTER_SECONDS, NUDGE_TEMPLATE
from .runner_process import OwnerRequest

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class RouteApplier:
    """Makes the routed message visible only after its obligation is decided."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def apply(self, application: RouteApplication) -> None:
        message, decision = application.message, application.decision
        cancelled = await self._runner._exchanges.cancel(decision.cancel_ids)
        target = await self._target(application, cancelled)
        message = replace(message, exchange_id=target.exchange_id)
        self._runner._runtime.narrative.append(message)
        if message.id is not None and target.exchange_id is not None:
            await self._runner._stores.messages.set_exchange(message.id, target.exchange_id)
        if target.refused:
            await self._runner._exchanges.reject_for_limit(message)
            return
        if target.exchange_id is not None:
            await self._runner._exchanges.ensure_owner(
                OwnerRequest(
                    target.exchange_id,
                    message,
                    application.command.client_message_id,
                    frozenset(cancelled),
                    application.command.cancel_epoch,
                    target.created,
                )
            )
        await self._nudge(target.exchange_id, application.live, cancelled)

    async def _target(self, application: RouteApplication, cancelled: set[str]) -> RouteTarget:
        decision = application.decision
        if decision.action is RouteAction.CONTINUE and decision.exchange_id is not None:
            await self._retitle(decision.exchange_id, decision.title)
            return RouteTarget(decision.exchange_id)
        if decision.action is RouteAction.COMMAND:
            return RouteTarget(None)
        at_limit = self._runner._process_registry.exceeds_limit(
            self._runner._exchanges.cancelled_tasks(cancelled)
        )
        if at_limit:
            return RouteTarget(None, refused=True)
        created = await self._runner._stores.exchanges.create(
            self._runner.dialog_id, application.message.content
        )
        return RouteTarget(created.id, created)

    async def _retitle(self, exchange_id: str, title: str | None) -> None:
        if title is None:
            return
        try:
            await self._runner._stores.exchanges.set_title(exchange_id, title)
        except Exception:
            logger.warning(
                "retitle failed: dialog=%s exchange=%s",
                self._runner.dialog_id,
                exchange_id,
                exc_info=True,
            )

    async def _nudge(
        self, current_id: str | None, live: list[Exchange], cancelled: set[str]
    ) -> None:
        now = utc_now()
        for exchange in live:
            waiting = (
                exchange.id != current_id
                and exchange.id not in cancelled
                and exchange.status is ExchangeStatus.AWAITING_USER
                and exchange.pending_question is not None
            )
            if not waiting or (now - exchange.updated_at).total_seconds() < NUDGE_AFTER_SECONDS:
                continue
            await self._runner._deliver_notice(
                NUDGE_TEMPLATE.format(title=exchange.title, question=exchange.pending_question)
            )
