"""Durable collection of forwarded material before it becomes an obligation."""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.dialogs.api import Exchange, ExchangeStatus
from octoforge_core.domain import ChatMessage

from .runner_commands import PromoteCollected, Submit
from .runner_constants import MATERIAL_TITLE_TEMPLATE
from .runner_text import untitled

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class MaterialCollector:
    """Collects bursts without starting one answer per forwarded message."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def collect(self, message: ChatMessage, command: Submit) -> None:
        exchange = await self._home(message, command.origin)
        message = replace(message, exchange_id=exchange.id)
        self._runner._runtime.narrative.append(message)
        if message.id is not None:
            await self._runner._stores.messages.set_exchange(message.id, exchange.id)
        await self._runner._stores.exchanges.touch(exchange.id)
        logger.info(
            "material collected: dialog=%s exchange=%s origin=%s",
            self._runner.dialog_id,
            exchange.id,
            command.origin,
        )

    async def _home(self, message: ChatMessage, origin: str | None) -> Exchange:
        answering = [
            item
            for item in await self._runner._stores.exchanges.list_live(self._runner.dialog_id)
            if self._runner._live_process_for(item.id) is not None
        ]
        if answering:
            return max(answering, key=lambda item: item.updated_at)
        return await self._collecting_exchange(message, origin)

    async def _collecting_exchange(self, message: ChatMessage, origin: str | None) -> Exchange:
        exchanges = self._runner._stores.exchanges
        existing = await exchanges.find_collecting(self._runner.dialog_id)
        if existing is not None:
            return existing
        title = MATERIAL_TITLE_TEMPLATE.format(origin=origin) if origin else untitled(message)
        return await exchanges.create(
            self._runner.dialog_id, title, status=ExchangeStatus.COLLECTING
        )

    async def nominate(self, exchange_id: str) -> None:
        await self._runner._runtime.inbox.put(PromoteCollected(exchange_id))
