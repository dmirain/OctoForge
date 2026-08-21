"""Cheap tariff checks and short-lived usage totals for every dialog on a node."""

import logging
import time
from datetime import datetime

from octoforge_core.tariffs.api import (
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
        return None if tariff is None else tariff.features

    async def allows(self, user_id: str, feature: str) -> bool:
        """Whether the user's tariff grants the feature (any code, core or custom)."""
        tariff = await self.resolve(user_id)
        return tariff is None or feature in tariff.features

    async def check_run_budget(self, user_id: str) -> LimitVerdict:
        """May an LLM run start today (user messages, answers, tokens)?

        The single budget choke point — every kind of run start asks this
        one question, so all three daily budgets are checked together.
        """
        tariff = await self.resolve(user_id)
        if tariff is None:
            return LimitVerdict.ok()
        totals = await self._totals_today(user_id)
        messages_cap = tariff.limits.daily_user_messages
        # The message is ledgered at intake, so its N-th run is still allowed.
        if messages_cap is not None and totals.user_messages > messages_cap:
            return LimitVerdict(
                allowed=False,
                reason="daily_user_messages",
                used=totals.user_messages,
                limit=messages_cap,
            )
        answers_cap = tariff.limits.daily_assistant_messages
        if answers_cap is not None and totals.assistant_messages >= answers_cap:
            return LimitVerdict(
                allowed=False,
                reason="daily_assistant_messages",
                used=totals.assistant_messages,
                limit=answers_cap,
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

    async def max_memory_chars(self, user_id: str) -> int | None:
        """The user's total memory-size cap in characters; `None` = unlimited."""
        tariff = await self.resolve(user_id)
        return None if tariff is None else tariff.limits.max_memory_chars

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
