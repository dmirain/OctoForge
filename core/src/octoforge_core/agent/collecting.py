"""Sweep promoting settled collections of forwarded material.

Forwarded material accumulates in a COLLECTING exchange and owes nobody an
answer yet: the user may still be forwarding, and a question may still be
coming. When a collection falls quiet, somebody has to decide that the
silence is final — that is this loop.

It is deliberately a nominator, not a writer: it finds candidates and asks
each dialog's actor to promote them. The actor re-checks the quiet window and
the status under its own serialization, so a promotion can never race a
question that is already starting a run on the same exchange.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from octoforge_core.dialogs.api import DialogRepository, ExchangeRepository

logger = logging.getLogger(__name__)

DEFAULT_QUIET_SECONDS = 30.0
DEFAULT_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class CollectingSources:
    """Repositories consulted by the collecting sweep."""

    exchanges: ExchangeRepository
    dialogs: DialogRepository


@dataclass(frozen=True, slots=True)
class CollectingSweepConfig:
    """Timing of the collecting sweep."""

    quiet_seconds: float = DEFAULT_QUIET_SECONDS
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS


DEFAULT_SWEEP_CONFIG = CollectingSweepConfig()


class CollectionPromoter(Protocol):
    """Port the sweep promotes through (the conversation manager implements it)."""

    async def promote_collection(self, user_id: str, channel: str, exchange_id: str) -> None:
        """Ask the dialog's actor to react to a settled collection."""
        ...


class CollectingSweeper:
    """Periodically hands settled material collections to their dialogs."""

    def __init__(
        self,
        sources: CollectingSources,
        promoter: CollectionPromoter,
        config: CollectingSweepConfig = DEFAULT_SWEEP_CONFIG,
    ) -> None:
        self._sources = sources
        self._promoter = promoter
        self._config = config

    async def run_forever(self) -> None:
        """Sweep until cancelled; a failing tick never stops the loop."""
        while True:
            try:
                await self.tick()
            except Exception:  # a sweep failure must not end the surface
                logger.exception("collecting sweep tick failed")
            await asyncio.sleep(self._config.interval_seconds)

    async def tick(self) -> int:
        """Nominate every settled collection; return how many were handed over."""
        stale = await self._sources.exchanges.list_stale_collecting(self._config.quiet_seconds)
        promoted = 0
        for exchange in stale:
            try:
                dialog = await self._sources.dialogs.get(exchange.dialog_id)
                await self._promoter.promote_collection(dialog.user_id, dialog.channel, exchange.id)
                promoted += 1
            except Exception:  # one broken dialog must not stall the others
                logger.exception("collection promotion failed: exchange=%s", exchange.id)
        return promoted
