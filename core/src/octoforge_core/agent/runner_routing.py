"""Deterministic-first exchange routing for submitted messages."""

import logging
from typing import TYPE_CHECKING

from octoforge_core.agent.router import ExchangeInfo, RouteAction, RouteDecision
from octoforge_core.dialogs.api import Exchange, ExchangeList, ExchangeStatus
from octoforge_core.domain import ChatMessage
from octoforge_core.time import utc_now

from .runner_commands import Submit

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class ExchangeRouter:
    """Chooses an obligation without an LLM whenever the answer is deterministic."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def route(
        self, message: ChatMessage, command: Submit, live: ExchangeList
    ) -> RouteDecision:
        live_ids = {item.id for item in live}
        if command.reply_to_exchange_id in live_ids:
            logger.info(
                "routed by reply: dialog=%s exchange=%s",
                self._runner.dialog_id,
                command.reply_to_exchange_id,
            )
            return RouteDecision(
                action=RouteAction.CONTINUE, exchange_id=command.reply_to_exchange_id
            )
        if not live:
            return RouteDecision()
        collection = self._sole_fresh_collection(live)
        if collection is not None:
            logger.info(
                "routed by collection: dialog=%s exchange=%s",
                self._runner.dialog_id,
                collection.id,
            )
            return RouteDecision(action=RouteAction.CONTINUE, exchange_id=collection.id)
        infos = tuple(self._exchange_info(item) for item in live)
        return await self._runner._config.router.route(
            infos, message.content, self._runner._config.max_processes
        )

    def _exchange_info(self, exchange: Exchange) -> ExchangeInfo:
        preview = None
        if exchange.status is ExchangeStatus.COLLECTING:
            preview = self._runner._material_promotion.preview(exchange.id)
        return ExchangeInfo(
            id=exchange.id,
            title=exchange.title,
            status=exchange.status,
            pending_question=exchange.pending_question,
            age_seconds=(utc_now() - exchange.updated_at).total_seconds(),
            preview=preview,
        )

    def _sole_fresh_collection(self, live: ExchangeList) -> Exchange | None:
        if len(live) != 1:
            return None
        collection = live[0]
        if collection.status is not ExchangeStatus.COLLECTING:
            return None
        quiet = (utc_now() - collection.updated_at).total_seconds()
        if quiet >= self._runner._config.material_quiet_seconds:
            return None
        return collection
