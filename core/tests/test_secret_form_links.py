"""Tests for the short-lived secrets-form codes."""

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.secrets.api import SecretFormPrefill, SecretPlacement, SecretTransform
from octoforge_core.secrets.link_store import SqlAlchemySecretFormLinkStore
from octoforge_core.secrets.models import SecretFormLinkRow
from octoforge_core.time import utc_now

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER = "person-1"
TTL = 600.0
# what the agent has to copy into a chat message; the token it replaced was
# ~700 characters, which a model rewrites wrong rather than copies
MAX_COPYABLE_CHARS = 24


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession]) -> SqlAlchemySecretFormLinkStore:
    return SqlAlchemySecretFormLinkStore(session_factory)


def prefill() -> SecretFormPrefill:
    return SecretFormPrefill(
        code="caldavicloud",
        allowed_host="*.icloud.com",
        description="app-specific password для календаря",
        placements=frozenset({SecretPlacement.HEADER, SecretPlacement.URL}),
        transform=SecretTransform.BASE64,
    )


async def test_code_is_short_and_opens_the_prefilled_form(
    store: SqlAlchemySecretFormLinkStore,
) -> None:
    code = await store.issue(USER, prefill(), TTL)

    session = await store.redeem(code)

    assert len(code) <= MAX_COPYABLE_CHARS
    assert session is not None
    assert session.user_id == USER
    assert session.prefill == prefill()


async def test_a_link_without_prefill_opens_an_empty_form(
    store: SqlAlchemySecretFormLinkStore,
) -> None:
    session = await store.redeem(await store.issue(USER, None, TTL))

    assert session is not None
    assert session.prefill is None


async def test_codes_are_unguessable_and_distinct(
    store: SqlAlchemySecretFormLinkStore,
) -> None:
    minted = 20
    codes = {await store.issue(USER, None, TTL) for _ in range(minted)}

    assert len(codes) == minted


async def test_an_unknown_code_buys_nothing(store: SqlAlchemySecretFormLinkStore) -> None:
    assert await store.redeem("nope") is None
    assert await store.is_expired("nope") is False  # never existed, not expired


async def test_an_expired_code_is_refused_but_recognized(
    store: SqlAlchemySecretFormLinkStore,
) -> None:
    """The two failures need different words: one is fixed by asking again."""
    code = await store.issue(USER, None, ttl_seconds=-1.0)

    assert await store.redeem(code) is None
    assert await store.is_expired(code) is True


async def test_issuing_sweeps_dead_rows(
    store: SqlAlchemySecretFormLinkStore, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """No sweeper for a table that only ever holds a handful of live rows."""
    await store.issue(USER, None, ttl_seconds=-1.0)
    live = await store.issue(USER, None, TTL)

    async with session_factory() as session:
        rows = (await session.scalars(select(SecretFormLinkRow))).all()

    assert [row.code for row in rows] == [live]


async def test_a_live_code_survives_a_sweep(
    store: SqlAlchemySecretFormLinkStore,
) -> None:
    code = await store.issue(USER, prefill(), TTL)
    await store.issue(USER, None, TTL)

    assert await store.redeem(code) is not None


async def test_expiry_is_stamped_from_the_ttl(
    store: SqlAlchemySecretFormLinkStore, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await store.issue(USER, None, TTL)

    async with session_factory() as session:
        (row,) = (await session.scalars(select(SecretFormLinkRow))).all()

    assert row.expires_at - row.created_at == timedelta(seconds=TTL)
    assert row.expires_at > utc_now()
