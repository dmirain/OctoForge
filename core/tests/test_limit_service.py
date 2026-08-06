"""Tests for LimitService: verdicts, feature sets and cache behavior."""

from datetime import datetime

from octoforge_core.tariffs.api import (
    FeatureCode,
    Tariff,
    TariffLimits,
    UsageEvent,
    UsageKind,
    UsageOrigin,
    UsageTotals,
)
from octoforge_core.tariffs.service import LimitService
from octoforge_core.time import utc_now

USER = "person-1"
NO_LIMITS = TariffLimits()
TOKEN_LIMIT = 1000
MESSAGE_LIMIT = 10
ANSWER_LIMIT = 5
CRON_CAP = 3
DATASET_CAP = 2


def _tariff(
    features: frozenset[FeatureCode] = frozenset({FeatureCode.WEB_SEARCH}),
    limits: TariffLimits = NO_LIMITS,
) -> Tariff:
    return Tariff(
        id="t-1",
        code="basic",
        title="Basic",
        features=features,
        limits=limits,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


class FakeTariffStore:
    def __init__(self, tariff: Tariff | None) -> None:
        self.tariff = tariff
        self.lookups = 0

    async def tariff_for_user(self, user_id: str) -> Tariff | None:
        self.lookups += 1
        return self.tariff


class FakeMeter:
    def __init__(self, totals: UsageTotals) -> None:
        self.totals = totals
        self.events: list[UsageEvent] = []
        self.reads = 0
        self.fail = False

    async def record(self, event: UsageEvent) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.events.append(event)

    async def totals_since(self, user_id: str, since: datetime) -> UsageTotals:
        self.reads += 1
        return self.totals


def _service(
    tariff: Tariff | None, totals: UsageTotals
) -> tuple[LimitService, FakeTariffStore, FakeMeter]:
    store = FakeTariffStore(tariff)
    meter = FakeMeter(totals)
    return LimitService(store, meter), store, meter  # type: ignore[arg-type]


async def test_no_tariff_means_no_restrictions() -> None:
    service, _, meter = _service(None, UsageTotals())

    assert (await service.check_run_budget(USER)).allowed
    assert await service.enabled_features(USER) is None
    assert await service.allows(USER, FeatureCode.VISION)
    assert await service.max_cron_jobs(USER) is None
    assert meter.reads == 0  # no tariff — the ledger is never consulted


async def test_run_budget_refused_over_the_message_limit() -> None:
    service, _, _ = _service(
        _tariff(limits=TariffLimits(daily_user_messages=MESSAGE_LIMIT)),
        UsageTotals(user_messages=MESSAGE_LIMIT + 1),
    )

    verdict = await service.check_run_budget(USER)

    assert not verdict.allowed
    assert verdict.reason == "daily_user_messages"
    assert verdict.used == MESSAGE_LIMIT + 1
    assert verdict.limit == MESSAGE_LIMIT


async def test_the_nth_message_under_a_limit_of_n_still_runs() -> None:
    """The message was ledgered at intake, before this check — the comparison
    is strict so the run it owes is not the one refused."""
    service, _, _ = _service(
        _tariff(limits=TariffLimits(daily_user_messages=MESSAGE_LIMIT)),
        UsageTotals(user_messages=MESSAGE_LIMIT),
    )

    assert (await service.check_run_budget(USER)).allowed


async def test_run_budget_refused_on_token_limit() -> None:
    service, _, _ = _service(
        _tariff(limits=TariffLimits(daily_tokens=TOKEN_LIMIT)),
        UsageTotals(prompt_tokens=TOKEN_LIMIT - 200, completion_tokens=200),
    )

    verdict = await service.check_run_budget(USER)

    assert not verdict.allowed
    assert verdict.reason == "daily_tokens"
    assert verdict.used == TOKEN_LIMIT
    assert verdict.limit == TOKEN_LIMIT


async def test_run_budget_allowed_under_limits() -> None:
    service, _, _ = _service(
        _tariff(limits=TariffLimits(daily_tokens=TOKEN_LIMIT, daily_user_messages=MESSAGE_LIMIT)),
        UsageTotals(prompt_tokens=TOKEN_LIMIT // 2, user_messages=MESSAGE_LIMIT - 1),
    )

    assert (await service.check_run_budget(USER)).allowed


async def test_run_budget_checks_assistant_messages() -> None:
    service, _, _ = _service(
        _tariff(limits=TariffLimits(daily_assistant_messages=ANSWER_LIMIT)),
        UsageTotals(assistant_messages=ANSWER_LIMIT),
    )

    verdict = await service.check_run_budget(USER)

    assert not verdict.allowed
    assert verdict.reason == "daily_assistant_messages"


async def test_features_and_caps() -> None:
    service, _, _ = _service(
        _tariff(
            features=frozenset({FeatureCode.SKILL_CREATE}),
            limits=TariffLimits(max_cron_jobs=CRON_CAP, max_datasets=DATASET_CAP),
        ),
        UsageTotals(),
    )

    assert await service.enabled_features(USER) == {"skill_create"}
    assert await service.allows(USER, FeatureCode.SKILL_CREATE)
    assert not await service.allows(USER, FeatureCode.VISION)
    assert not await service.allows(USER, "my_tool")  # custom codes gate the same way
    assert await service.max_cron_jobs(USER) == CRON_CAP
    assert await service.max_datasets(USER) == DATASET_CAP


async def test_totals_are_cached_but_the_tariff_is_not() -> None:
    """The sum-over-window is the costly read; the tariff lookup stays fresh
    so an operator's change applies at once."""
    service, store, meter = _service(
        _tariff(limits=TariffLimits(daily_tokens=TOKEN_LIMIT)), UsageTotals()
    )

    await service.check_run_budget(USER)
    await service.check_run_budget(USER)

    assert store.lookups == 1 + 1
    assert meter.reads == 1


async def test_record_invalidates_totals_and_never_raises() -> None:
    service, _, meter = _service(
        _tariff(limits=TariffLimits(daily_tokens=TOKEN_LIMIT)), UsageTotals()
    )
    event = UsageEvent(
        user_id=USER, kind=UsageKind.USER_MESSAGE, origin=UsageOrigin.INTERACTIVE, quantity=1
    )

    await service.check_run_budget(USER)
    await service.record(event)
    await service.check_run_budget(USER)

    assert meter.events == [event]
    assert meter.reads == 1 + 1  # the recorded event invalidated the cached totals

    meter.fail = True
    await service.record(event)  # a metering failure must not surface
