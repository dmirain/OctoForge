"""Tests for the tariff catalog store and the usage ledger."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.tariffs.api import (
    FeatureCode,
    InvalidTariffError,
    TariffInUseError,
    TariffLimits,
    TariffNotFoundError,
    UsageEvent,
    UsageKind,
    UsageOrigin,
)
from octoforge_core.tariffs.store import SqlAlchemyTariffStore, SqlAlchemyUsageMeter
from octoforge_core.time import utc_now

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "person-1"
USER_B = "person-2"
TOKEN_LIMIT = 1000
MESSAGE_LIMIT = 50

ANSWER_TODAY = UsageEvent(
    user_id=USER_A,
    kind=UsageKind.LLM_ANSWER,
    origin=UsageOrigin.INTERACTIVE,
    prompt_tokens=100,
    completion_tokens=40,
    quantity=1,
    dialog_id="dialog-1",
    exchange_id="exchange-1",
    task_id="task-1",
)
ROUTING_TODAY = UsageEvent(
    user_id=USER_A,
    kind=UsageKind.LLM_ROUTING,
    origin=UsageOrigin.INTERACTIVE,
    prompt_tokens=20,
    completion_tokens=5,
)
MESSAGE_TODAY = UsageEvent(
    user_id=USER_A,
    kind=UsageKind.USER_MESSAGE,
    origin=UsageOrigin.INTERACTIVE,
    quantity=1,
)


@pytest.fixture
async def stores() -> AsyncIterator[tuple[SqlAlchemyTariffStore, SqlAlchemyUsageMeter]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    factory = create_session_factory(engine)
    yield SqlAlchemyTariffStore(factory), SqlAlchemyUsageMeter(factory)
    await engine.dispose()


@pytest.fixture
async def tariffs(
    stores: tuple[SqlAlchemyTariffStore, SqlAlchemyUsageMeter],
) -> SqlAlchemyTariffStore:
    return stores[0]


@pytest.fixture
async def meter(
    stores: tuple[SqlAlchemyTariffStore, SqlAlchemyUsageMeter],
) -> SqlAlchemyUsageMeter:
    return stores[1]


async def test_put_and_replace(tariffs: SqlAlchemyTariffStore) -> None:
    created = await tariffs.put(
        "basic",
        "Basic",
        frozenset({FeatureCode.WEB_SEARCH}),
        TariffLimits(daily_tokens=TOKEN_LIMIT),
    )
    replaced = await tariffs.put(
        "basic",
        "Basic v2",
        frozenset({FeatureCode.WEB_SEARCH, FeatureCode.VISION}),
        TariffLimits(daily_user_messages=MESSAGE_LIMIT),
    )

    assert replaced.id == created.id
    assert replaced.title == "Basic v2"
    assert replaced.features == {FeatureCode.WEB_SEARCH, FeatureCode.VISION}
    # a put replaces every limit, so the token cap from the first put is gone
    assert replaced.limits == TariffLimits(daily_user_messages=MESSAGE_LIMIT)
    assert [tariff.code for tariff in await tariffs.list()] == ["basic"]


async def test_custom_feature_codes_roundtrip(tariffs: SqlAlchemyTariffStore) -> None:
    """Installer-defined codes are plain strings: the store accepts a code it
    has never heard of — the vocabulary check lives at the operator boundary,
    where the assembly's merged feature set is known."""
    created = await tariffs.put("ext", "Ext", frozenset({"my_tool", FeatureCode.VISION}))

    assert created.features == {"my_tool", "vision"}
    with pytest.raises(InvalidTariffError):  # grammar is still enforced
        await tariffs.put("ext", "Ext", frozenset({"Bad Feature!"}))


async def test_validation(tariffs: SqlAlchemyTariffStore) -> None:
    with pytest.raises(InvalidTariffError):
        await tariffs.put("Bad Code!", "x", frozenset())
    with pytest.raises(InvalidTariffError):
        await tariffs.put("basic", "", frozenset())
    with pytest.raises(InvalidTariffError):
        await tariffs.put("basic", "Basic", frozenset(), TariffLimits(daily_tokens=-1))


async def test_assignment_lifecycle(tariffs: SqlAlchemyTariffStore) -> None:
    await tariffs.put("basic", "Basic", frozenset(), TariffLimits(daily_tokens=TOKEN_LIMIT))
    await tariffs.put("pro", "Pro", frozenset({FeatureCode.SKILL_CREATE}))

    assert await tariffs.tariff_for_user(USER_A) is None

    await tariffs.assign(USER_A, "basic")
    bound = await tariffs.tariff_for_user(USER_A)
    assert bound is not None and bound.code == "basic"
    assert await tariffs.tariff_for_user(USER_B) is None

    await tariffs.assign(USER_A, "pro")  # rebinding replaces, not duplicates
    rebound = await tariffs.tariff_for_user(USER_A)
    assert rebound is not None and rebound.code == "pro"
    assert await tariffs.assignments() == {USER_A: "pro"}

    await tariffs.assign(USER_A, None)
    assert await tariffs.tariff_for_user(USER_A) is None
    assert await tariffs.assignments() == {}

    with pytest.raises(TariffNotFoundError):
        await tariffs.assign(USER_A, "missing")


async def test_delete_refuses_while_assigned(tariffs: SqlAlchemyTariffStore) -> None:
    await tariffs.put("basic", "Basic", frozenset())
    await tariffs.assign(USER_A, "basic")

    with pytest.raises(TariffInUseError):
        await tariffs.delete("basic")

    await tariffs.assign(USER_A, None)
    await tariffs.delete("basic")
    assert await tariffs.list() == []
    with pytest.raises(TariffNotFoundError):
        await tariffs.delete("basic")


async def test_ledger_totals_and_window(meter: SqlAlchemyUsageMeter) -> None:
    now = utc_now()
    yesterday_answer = UsageEvent(
        user_id=USER_A,
        kind=UsageKind.LLM_ANSWER,
        origin=UsageOrigin.CRON,
        prompt_tokens=999,
        completion_tokens=999,
        quantity=1,
        created_at=now - timedelta(days=1),
    )
    for event in (ANSWER_TODAY, ROUTING_TODAY, MESSAGE_TODAY, yesterday_answer):
        await meter.record(event)

    totals = await meter.totals_since(USER_A, now - timedelta(hours=1))
    today_tokens = (
        ANSWER_TODAY.prompt_tokens
        + ANSWER_TODAY.completion_tokens
        + ROUTING_TODAY.prompt_tokens
        + ROUTING_TODAY.completion_tokens
    )

    assert totals.prompt_tokens == ANSWER_TODAY.prompt_tokens + ROUTING_TODAY.prompt_tokens
    assert totals.tokens == today_tokens
    assert totals.user_messages == 1
    assert totals.assistant_messages == 1

    wide = await meter.totals_since(USER_A, now - timedelta(days=2))
    yesterday_tokens = yesterday_answer.prompt_tokens + yesterday_answer.completion_tokens
    assert wide.assistant_messages == 1 + 1
    assert wide.tokens == today_tokens + yesterday_tokens


async def test_ledger_owner_isolation_and_empty(meter: SqlAlchemyUsageMeter) -> None:
    await meter.record(MESSAGE_TODAY)

    totals = await meter.totals_since(USER_B, utc_now() - timedelta(hours=1))

    assert totals.tokens == 0
    assert totals.user_messages == 0
    assert totals.assistant_messages == 0
