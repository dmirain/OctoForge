"""Tests for the Telegram invite store on in-memory SQLite."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC

import pytest
from octoforge_core.db.engine import create_engine, create_session_factory
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_web.telegram.invites.api import (
    InviteAlreadyClaimedError,
    InviteExpiredError,
    InviteNotFoundError,
    InviteStatus,
)
from octoforge_web.telegram.invites.store import SqlAlchemyInviteStore, SqlAlchemyMemberDirectory
from octoforge_web.telegram.schema import TelegramSurfaceBase

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
USER_ID = "tg:111"
OTHER_USER_ID = "tg:222"
NOTE = "for Alice"
CRON_JOB_IDS = ("job-1", "job-2")


@pytest.fixture
async def store() -> AsyncIterator[SqlAlchemyInviteStore]:
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    yield SqlAlchemyInviteStore(session_factory)
    await engine.dispose()


@pytest.fixture
async def expiring_store() -> AsyncIterator[SqlAlchemyInviteStore]:
    """Store with a zero TTL: every pending code is already expired."""
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    yield SqlAlchemyInviteStore(session_factory, ttl_seconds=0)
    await engine.dispose()


async def test_create_returns_pending_invite_with_code(store: SqlAlchemyInviteStore) -> None:
    invite = await store.create(NOTE)

    assert invite.status is InviteStatus.PENDING
    assert invite.code
    assert invite.note == NOTE
    assert invite.created_at.tzinfo == UTC
    assert invite.claimed_by is None


async def test_claim_marks_the_code_claimed(store: SqlAlchemyInviteStore) -> None:
    invite = await store.create(NOTE)

    claimed = await store.claim(invite.code, USER_ID)

    assert claimed.status is InviteStatus.CLAIMED
    assert claimed.claimed_by == USER_ID
    assert claimed.claimed_at is not None and claimed.claimed_at.tzinfo == UTC
    by_user = await store.get_by_user(USER_ID)
    assert by_user is not None and by_user.id == invite.id


async def test_claim_unknown_code_raises(store: SqlAlchemyInviteStore) -> None:
    with pytest.raises(InviteNotFoundError):
        await store.claim("no-such-code", USER_ID)


async def test_claim_twice_raises_already_claimed(store: SqlAlchemyInviteStore) -> None:
    invite = await store.create(NOTE)
    await store.claim(invite.code, USER_ID)

    with pytest.raises(InviteAlreadyClaimedError):
        await store.claim(invite.code, OTHER_USER_ID)


async def test_concurrent_claims_only_one_wins(store: SqlAlchemyInviteStore) -> None:
    invite = await store.create(NOTE)

    results = await asyncio.gather(
        store.claim(invite.code, USER_ID),
        store.claim(invite.code, OTHER_USER_ID),
        return_exceptions=True,
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, InviteAlreadyClaimedError)]
    assert len(successes) == 1
    assert len(failures) == 1


async def test_revoke_and_restore_round_trip(store: SqlAlchemyInviteStore) -> None:
    invite = await store.create(NOTE)
    await store.claim(invite.code, USER_ID)

    revoked = await store.revoke(invite.id)

    assert revoked.status is InviteStatus.REVOKED
    assert revoked.revoked_at is not None and revoked.revoked_at.tzinfo == UTC

    restored = await store.restore(invite.id)

    assert restored.status is InviteStatus.CLAIMED
    assert restored.claimed_by == USER_ID
    assert restored.revoked_at is None


async def test_revoke_unknown_invite_raises(store: SqlAlchemyInviteStore) -> None:
    with pytest.raises(InviteNotFoundError):
        await store.revoke("no-such-id")


async def test_disabled_cron_jobs_round_trip(store: SqlAlchemyInviteStore) -> None:
    invite = await store.create(NOTE)
    await store.claim(invite.code, USER_ID)

    await store.set_disabled_cron_jobs(invite.id, CRON_JOB_IDS)

    fetched = await store.get_by_user(USER_ID)
    assert fetched is not None
    assert fetched.disabled_cron_job_ids == CRON_JOB_IDS

    await store.set_disabled_cron_jobs(invite.id, ())

    cleared = await store.get_by_user(USER_ID)
    assert cleared is not None
    assert cleared.disabled_cron_job_ids == ()


async def test_list_all_includes_every_status(store: SqlAlchemyInviteStore) -> None:
    pending = await store.create("one")
    claimed = await store.create("two")
    await store.claim(claimed.code, USER_ID)
    revoked = await store.create("three")
    await store.revoke(revoked.id)

    invites = await store.list_all()

    assert [invite.id for invite in invites] == [pending.id, claimed.id, revoked.id]
    assert [invite.status for invite in invites] == [
        InviteStatus.PENDING,
        InviteStatus.CLAIMED,
        InviteStatus.REVOKED,
    ]


async def test_expired_code_cannot_be_claimed(
    expiring_store: SqlAlchemyInviteStore,
) -> None:
    invite = await expiring_store.create(NOTE)

    with pytest.raises(InviteExpiredError):
        await expiring_store.claim(invite.code, USER_ID)

    fetched = await expiring_store.get_by_code(invite.code)
    assert fetched is not None
    assert fetched.status is InviteStatus.PENDING  # expired, not claimed


async def test_store_without_ttl_claims_regardless_of_age(
    store: SqlAlchemyInviteStore,
) -> None:
    invite = await store.create(NOTE)

    claimed = await store.claim(invite.code, USER_ID)

    assert claimed.status is InviteStatus.CLAIMED


@pytest.fixture
async def directory() -> AsyncIterator[SqlAlchemyMemberDirectory]:
    engine = create_engine(MEMORY_DATABASE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(TelegramSurfaceBase.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    yield SqlAlchemyMemberDirectory(session_factory)
    await engine.dispose()


async def test_record_creates_then_refreshes_the_profile(
    directory: SqlAlchemyMemberDirectory,
) -> None:
    await directory.record(USER_ID, "Alice", "Smith", "alice")
    created = await directory.get(USER_ID)
    assert created is not None
    assert created.display_name == "Alice Smith (@alice)"
    assert created.first_seen_at == created.last_seen_at

    await directory.record(USER_ID, "Alicia", "Smith", None)
    updated = await directory.get(USER_ID)
    assert updated is not None
    assert updated.first_name == "Alicia"
    assert updated.username is None
    assert updated.display_name == "Alicia Smith"
    assert updated.first_seen_at == created.first_seen_at  # entry moment is kept
    assert updated.last_seen_at >= created.last_seen_at


async def test_unknown_member_is_none_and_listing_is_recent_first(
    directory: SqlAlchemyMemberDirectory,
) -> None:
    assert await directory.get("tg:404") is None
    await directory.record(USER_ID, "Alice", "", None)
    await directory.record(OTHER_USER_ID, "Bob", "", "bob")
    await directory.record(USER_ID, "Alice", "", None)  # Alice seen again, later
    listed = await directory.list_all()
    assert [profile.user_id for profile in listed] == [USER_ID, OTHER_USER_ID]
    assert listed[1].display_name == "Bob (@bob)"
