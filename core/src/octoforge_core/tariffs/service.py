"""Limit checks and metering over the tariff catalog and the usage ledger.

One instance serves every dialog on the node, so a check must stay cheap. The
tariff lookup is a single SELECT by unique key per user action (a message, a
run start) and is deliberately uncached — an operator's change applies at
once. The day's totals are the sum-over-window query, so they are cached for
a few seconds and invalidated by the node's own metering; cross-node spend
converges within the TTL — limits are guardrails, not accounting.
"""

import logging
import time
from datetime import datetime

from octoforge_core.tariffs.api import (
    FeatureCode,
    LimitVerdict,
    Tariff,
    TariffStore,
    UsageEvent,
    UsageMeter,
    UsageTotals,
)
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)


class LimitService:
    """Resolves a user's tariff, checks its limits and meters consumption."""

    def __init__(
        self,
        tariffs: TariffStore,
        meter: UsageMeter,
        *,
        verdict_cache_ttl: float = 5.0,
    ) -> None:
        self._tariffs = tariffs
        self._meter = meter
        self._totals_ttl = verdict_cache_ttl
        self._totals_cache: dict[str, tuple[float, datetime, UsageTotals]] = {}

    async def resolve(self, user_id: str) -> Tariff | None:
        """Return the user's tariff; `None` means no restrictions."""
        return await self._tariffs.tariff_for_user(user_id)

    async def enabled_features(self, user_id: str) -> frozenset[str] | None:
        """The user's feature codes as plain strings; `None` = everything on."""
        tariff = await self.resolve(user_id)
        if tariff is None:
            return None
        return frozenset(feature.value for feature in tariff.features)

    async def allows(self, user_id: str, feature: FeatureCode) -> bool:
        """Whether the user's tariff grants the feature."""
        tariff = await self.resolve(user_id)
        return tariff is None or feature in tariff.features

    async def check_submit(self, user_id: str) -> LimitVerdict:
        """May the user's message start a run today (messages + tokens)?"""
        tariff = await self.resolve(user_id)
        if tariff is None:
            return LimitVerdict.ok()
        totals = await self._totals_today(user_id)
        if tariff.limits.daily_user_messages is not None and (
            totals.user_messages >= tariff.limits.daily_user_messages
        ):
            return LimitVerdict(
                allowed=False,
                reason="daily_user_messages",
                used=totals.user_messages,
                limit=tariff.limits.daily_user_messages,
            )
        return self._check_tokens(tariff, totals)

    async def check_run_budget(self, user_id: str) -> LimitVerdict:
        """May a cron/background run start today (answers + tokens)?"""
        tariff = await self.resolve(user_id)
        if tariff is None:
            return LimitVerdict.ok()
        totals = await self._totals_today(user_id)
        if tariff.limits.daily_assistant_messages is not None and (
            totals.assistant_messages >= tariff.limits.daily_assistant_messages
        ):
            return LimitVerdict(
                allowed=False,
                reason="daily_assistant_messages",
                used=totals.assistant_messages,
                limit=tariff.limits.daily_assistant_messages,
            )
        return self._check_tokens(tariff, totals)

    async def max_cron_jobs(self, user_id: str) -> int | None:
        """The user's cron-job cap; `None` = unlimited."""
        tariff = await self.resolve(user_id)
        return None if tariff is None else tariff.limits.max_cron_jobs

    async def max_datasets(self, user_id: str) -> int | None:
        """The user's dataset cap; `None` = unlimited."""
        tariff = await self.resolve(user_id)
        return None if tariff is None else tariff.limits.max_datasets

    async def record(self, event: UsageEvent) -> None:
        """Append a usage event; a metering failure never fails the caller."""
        try:
            await self._meter.record(event)
        except Exception:
            logger.exception("usage event lost: %s for user %s", event.kind, event.user_id)
            return
        self._totals_cache.pop(event.user_id, None)

    async def _totals_today(self, user_id: str) -> UsageTotals:
        day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
        cached = self._totals_cache.get(user_id)
        if cached is not None and cached[0] > time.monotonic() and cached[1] == day_start:
            return cached[2]
        totals = await self._meter.totals_since(user_id, day_start)
        self._totals_cache[user_id] = (time.monotonic() + self._totals_ttl, day_start, totals)
        return totals

    @staticmethod
    def _check_tokens(tariff: Tariff, totals: UsageTotals) -> LimitVerdict:
        if tariff.limits.daily_tokens is not None and totals.tokens >= tariff.limits.daily_tokens:
            return LimitVerdict(
                allowed=False,
                reason="daily_tokens",
                used=totals.tokens,
                limit=tariff.limits.daily_tokens,
            )
        return LimitVerdict.ok()
