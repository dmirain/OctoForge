"""Quiet-window promotion and reparenting of collected material."""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.agent.router import ExchangeInfo, RouteAction
from octoforge_core.dialogs.api import Exchange, ExchangeNotFoundError, ExchangeStatus
from octoforge_core.domain import MessageKind
from octoforge_core.time import utc_now

from .runner_commands import PromoteCollected
from .runner_constants import (
    MATERIAL_DIGEST_CHARS,
    MATERIAL_DIGEST_MESSAGES,
    MATERIAL_DIGEST_TEMPLATE,
)
from .runner_text import bounded_preview

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class MaterialPromoter:
    """Turns a settled batch into one obligation or attaches it to another."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def promote(self, command: PromoteCollected) -> None:
        try:
            exchange = await self._runner._stores.exchanges.get(command.exchange_id)
        except ExchangeNotFoundError:
            return
        quiet = (utc_now() - exchange.updated_at).total_seconds()
        if exchange.status is not ExchangeStatus.COLLECTING:
            return
        if quiet < self._runner._config.material_quiet_seconds:
            return
        target = await self._target(exchange)
        if target is not None:
            await self._reparent(exchange, target)
            return
        await self._runner._stores.exchanges.set_status(exchange.id, ExchangeStatus.OPEN)
        await self._runner._exchanges.resume_open(exchange.id, notify_limit=False)

    async def _target(self, collection: Exchange) -> Exchange | None:
        live = [
            item
            for item in await self._runner._stores.exchanges.list_live(self._runner.dialog_id)
            if item.id != collection.id and item.status is not ExchangeStatus.COLLECTING
        ]
        if not live:
            return None
        infos = tuple(
            ExchangeInfo(
                id=item.id,
                title=item.title,
                status=item.status,
                pending_question=item.pending_question,
                age_seconds=(utc_now() - item.updated_at).total_seconds(),
            )
            for item in live
        )
        decision = await self._runner._config.router.route(
            infos, self.digest(collection.id), self._runner._config.max_processes
        )
        await self._runner._usage.record_routing(decision.usage)
        if decision.action is not RouteAction.CONTINUE or decision.exchange_id is None:
            return None
        return next((item for item in live if item.id == decision.exchange_id), None)

    def digest(self, exchange_id: str) -> str:
        pieces = self.pieces(exchange_id)
        return MATERIAL_DIGEST_TEMPLATE.format(
            count=len(pieces), lines=bounded_preview(pieces, MATERIAL_DIGEST_CHARS)
        )

    def preview(self, exchange_id: str) -> str | None:
        pieces = self.pieces(exchange_id)
        return bounded_preview(pieces, MATERIAL_DIGEST_CHARS) if pieces else None

    def pieces(self, exchange_id: str) -> list[str]:
        return [
            message.content
            for message in self._runner._runtime.narrative
            if message.exchange_id == exchange_id and message.kind is MessageKind.MATERIAL
        ][:MATERIAL_DIGEST_MESSAGES]

    async def _reparent(self, collection: Exchange, target: Exchange) -> None:
        for index, message in enumerate(self._runner._runtime.narrative):
            if message.exchange_id != collection.id:
                continue
            self._runner._runtime.narrative[index] = replace(message, exchange_id=target.id)
            if message.id is not None:
                await self._runner._stores.messages.set_exchange(message.id, target.id)
        await self._runner._stores.exchanges.set_status(collection.id, ExchangeStatus.CANCELLED)
        await self._runner._stores.exchanges.touch(target.id)
        if self._runner._live_process_for(target.id) is None:
            await self._runner._stores.exchanges.set_status(target.id, ExchangeStatus.OPEN)
            await self._runner._exchanges.resume_open(target.id, notify_limit=False)
