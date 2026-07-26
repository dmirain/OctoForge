"""Tests for the encrypted per-user secret store."""

from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.secrets.api import (
    InvalidSecretError,
    SecretHostMismatchError,
    SecretNotFoundError,
)
from octoforge_core.secrets.models import SecretRow
from octoforge_core.secrets.store import SqlAlchemySecretStore

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_A = "tg:1"
USER_B = "tg:2"
CODE = "gmail_token"
VALUE = "ya29.a0-very-secret-token"
HOST = "gmail.googleapis.com"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def store(session_factory: async_sessionmaker[AsyncSession], key: str) -> SqlAlchemySecretStore:
    return SqlAlchemySecretStore(session_factory, key)


async def test_roundtrip_and_last_used_stamp(store: SqlAlchemySecretStore) -> None:
    await store.put(USER_A, CODE, VALUE, HOST)

    resolved = await store.resolve(USER_A, CODE, HOST)
    (info,) = await store.list(USER_A)

    assert resolved == VALUE
    assert (info.code, info.allowed_host) == (CODE, HOST)
    assert info.last_used_at is not None


async def test_value_is_encrypted_at_rest(
    store: SqlAlchemySecretStore, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Neither the database nor its dumps may contain the plaintext."""
    await store.put(USER_A, CODE, VALUE, HOST)

    async with session_factory() as session:
        (row,) = (await session.scalars(select(SecretRow))).all()

    assert VALUE not in row.ciphertext


async def test_listing_never_carries_values(store: SqlAlchemySecretStore) -> None:
    await store.put(USER_A, CODE, VALUE, HOST)

    (info,) = await store.list(USER_A)

    assert not hasattr(info, "value")
    assert VALUE not in repr(info)


async def test_host_binding_blocks_other_hosts(store: SqlAlchemySecretStore) -> None:
    """The exfiltration guard: the value never travels to a foreign host."""
    await store.put(USER_A, CODE, VALUE, HOST)

    with pytest.raises(SecretHostMismatchError) as denied:
        await store.resolve(USER_A, CODE, "evil.example.com")

    assert VALUE not in str(denied.value)  # the error text must not leak it


async def test_put_replaces_and_resets_usage(store: SqlAlchemySecretStore) -> None:
    await store.put(USER_A, CODE, VALUE, HOST)
    await store.resolve(USER_A, CODE, HOST)

    await store.put(USER_A, CODE, "rotated-value", "api.example.com")
    (info,) = await store.list(USER_A)

    assert info.allowed_host == "api.example.com"
    assert info.last_used_at is None
    assert await store.resolve(USER_A, CODE, "api.example.com") == "rotated-value"


async def test_owner_isolation(store: SqlAlchemySecretStore) -> None:
    await store.put(USER_A, CODE, VALUE, HOST)

    assert await store.list(USER_B) == []
    with pytest.raises(SecretNotFoundError):
        await store.resolve(USER_B, CODE, HOST)


async def test_delete(store: SqlAlchemySecretStore) -> None:
    await store.put(USER_A, CODE, VALUE, HOST)
    await store.delete(USER_A, CODE)

    assert await store.list(USER_A) == []
    with pytest.raises(SecretNotFoundError):
        await store.delete(USER_A, CODE)


async def test_wrong_key_reads_as_missing(
    session_factory: async_sessionmaker[AsyncSession], key: str
) -> None:
    """A rotated master key must degrade to \"re-enter the secret\", not crash."""
    await SqlAlchemySecretStore(session_factory, key).put(USER_A, CODE, VALUE, HOST)
    other = SqlAlchemySecretStore(session_factory, Fernet.generate_key().decode())

    with pytest.raises(SecretNotFoundError):
        await other.resolve(USER_A, CODE, HOST)


async def test_validation(store: SqlAlchemySecretStore) -> None:
    with pytest.raises(InvalidSecretError):
        await store.put(USER_A, "Bad Code!", VALUE, HOST)
    with pytest.raises(InvalidSecretError):
        await store.put(USER_A, CODE, "", HOST)
    with pytest.raises(InvalidSecretError):
        await store.put(USER_A, CODE, VALUE, "https://host/with/path")
    with pytest.raises(InvalidSecretError):  # header injection guard
        await store.put(USER_A, CODE, "tok\r\nX-Evil: 1", HOST)
    with pytest.raises(InvalidSecretError):  # non-ASCII cannot travel in a header
        await store.put(USER_A, CODE, "секрет", HOST)
    # normalization: case and trailing dot fold away
    await store.put(USER_A, "  GMAIL_TOKEN ".strip().lower(), VALUE, "Gmail.googleapis.com.")
    assert (await store.list(USER_A))[0].allowed_host == HOST


def test_malformed_key_fails_at_construction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError):
        SqlAlchemySecretStore(session_factory, "not-a-fernet-key")
