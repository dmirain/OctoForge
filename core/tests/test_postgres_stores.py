"""Store contracts against a real Postgres; skipped without `OF_TEST_DATABASE_URL`.

The other 28 database test modules run on SQLite — fast, no daemon — and that
is deliberate. This module exists because the two dialects diverge exactly
where the stores are subtle, and a SQLite-only suite cannot see any of it:

- `UTCDateTime` renders a different column type per dialect (`timestamptz` here,
  naive elsewhere) and asyncpg rejects a mismatched bind outright;
- the partial unique indexes (`uq_memories_global_key`,
  `uq_instructions_public_type_title`) need `postgresql_where`, or Postgres
  silently builds a *full* unique index and breaks global memory keys and
  private-over-public instruction shadowing;
- a failed statement aborts the whole Postgres transaction, so the
  find-then-insert and seq-collision retries only work because they roll back;
- `bootstrap_schema` takes its non-SQLite branch here (create_all + stamp head).

Run with: `make test-pg` (starts the compose service and points this at it).
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_core.cron.api import CronJob
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.datasets.api import DatasetSchema
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.db.engine import bootstrap_schema, create_engine, create_session_factory
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.dialogs.store import SqlAlchemyDialogRepository, SqlAlchemyMessageRepository
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.instructions.api import InstructionDraft, InstructionType
from octoforge_core.instructions.store import SqlAlchemyInstructionStore

DATABASE_URL_ENV = "OF_TEST_DATABASE_URL"
DATABASE_URL = os.environ.get(DATABASE_URL_ENV, "")
TEST_DATABASE_MARKER = "test"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason=f"{DATABASE_URL_ENV} is not set (Postgres store tests)"
)


def _guard_database_name(url: str) -> None:
    """Refuse to run against a database whose name does not say "test".

    The fixture below drops the whole `public` schema, so a URL pointing at the
    application database would wipe it. `make test-pg` targets `octoforge_test`.
    """
    name = make_url(url).database or ""
    if TEST_DATABASE_MARKER not in name:
        raise pytest.UsageError(
            f"{DATABASE_URL_ENV} points at database {name!r}, which does not look like a test "
            f"database (name must contain {TEST_DATABASE_MARKER!r}); these tests drop its schema"
        )


USER_A = "user-a"
USER_B = "user-b"
CHANNEL = "web"
OWNER = "owner-1"
SHARED_KEY = "diet"
GLOBAL_CONTENT = "global note"
EMBEDDING = (0.5, 0.5)
TITLE = "get_weather"
NOW = datetime(2026, 7, 25, 9, 30, tzinfo=UTC)
LEASE_STALE_AFTER = timedelta(minutes=1)
EXPECTED_TABLES = frozenset(
    {
        "alembic_version",
        "cron_jobs",
        "dataset_records",
        "datasets",
        "dialog_summaries",
        "dialogs",
        "instructions",
        "messages",
        "tasks",
    }
)
TWO_APPENDS = 2
SECOND_VERSION = 2
SEARCH_LIMIT = 10
SCAN_LIMIT = 50


@pytest.fixture
async def pristine_engine() -> AsyncIterator[AsyncEngine]:
    """An engine over an empty database: the schema is dropped before each test.

    `Base.metadata.drop_all` would leave `alembic_version` behind, and the
    bootstrap test needs a truly empty database, so the schema is recreated.
    """
    _guard_database_name(DATABASE_URL)
    engine = create_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    yield engine
    await engine.dispose()


@pytest.fixture
async def engine(pristine_engine: AsyncEngine) -> AsyncEngine:
    """A bootstrapped database (also the non-SQLite bootstrap path under test)."""
    await bootstrap_schema(pristine_engine)
    return pristine_engine


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


def cron_job(job_id: str, *, next_fire_at: datetime, claimed_at: datetime | None = None) -> CronJob:
    return CronJob(
        id=job_id,
        user_id=USER_A,
        channel=CHANNEL,
        title="morning report",
        schedule="0 9 * * *",
        timezone="UTC",
        prompt="prepare the report",
        enabled=True,
        next_fire_at=next_fire_at,
        last_fire_at=None,
        claimed_by="dead-instance" if claimed_at is not None else None,
        claimed_at=claimed_at,
        created_at=NOW,
        one_shot=False,
        last_status=None,
        last_error=None,
        retry_count=0,
    )


def draft(owner_id: str | None, title: str = TITLE) -> InstructionDraft:
    return InstructionDraft(
        kind=InstructionType.SKILL,
        title=title,
        content="do the thing",
        tags=("test",),
        embedding=EMBEDDING,
        owner_id=owner_id,
    )


def memory_draft(
    key: str,
    content: str,
    owner_id: str | None,
    embedding: tuple[float, ...] = EMBEDDING,
) -> InstructionDraft:
    return InstructionDraft(
        kind=InstructionType.MEMORY,
        title=key,
        content=content,
        tags=(),
        embedding=embedding,
        owner_id=owner_id,
    )


async def test_bootstrap_creates_the_schema_and_stamps_head(
    pristine_engine: AsyncEngine,
) -> None:
    """A fresh Postgres database gets today's schema and one version row."""
    await bootstrap_schema(pristine_engine)

    async with pristine_engine.connect() as connection:
        tables = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
        versions = (await connection.execute(text("SELECT version_num FROM alembic_version"))).all()
    assert tables >= EXPECTED_TABLES
    assert len(versions) == 1


async def test_bootstrap_is_idempotent(engine: AsyncEngine) -> None:
    """A second startup over the same database must be a no-op, not an error."""
    await bootstrap_schema(engine)

    async with engine.connect() as connection:
        versions = (await connection.execute(text("SELECT version_num FROM alembic_version"))).all()
    assert len(versions) == 1


async def test_datetime_columns_are_timezone_aware(engine: AsyncEngine) -> None:
    """`UTCDateTime` must map to timestamptz here, or aware binds would fail.

    Asserted against `information_schema`, not the reflected SQLAlchemy type:
    `str(TIMESTAMP(timezone=True))` renders plain "TIMESTAMP" and would pass
    even if the column had no timezone.
    """
    async with engine.connect() as connection:
        data_types = (
            await connection.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'dialogs' AND column_name IN ('created_at', 'updated_at')"
                )
            )
        ).scalars()

    assert set(data_types) == {"timestamp with time zone"}


async def test_datetimes_round_trip_as_aware_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_A, CHANNEL)
    await SqlAlchemyMessageRepository(session_factory).append(
        dialog.id, ChatMessage(role=MessageRole.USER, content="hi")
    )

    async with session_factory() as session:
        row = (await session.scalars(select(MessageRow))).one()
        stored_dialog = (await session.scalars(select(DialogRow))).one()

    assert row.created_at.tzinfo is not None
    assert row.created_at.utcoffset() == timedelta(0)
    assert stored_dialog.created_at.tzinfo is not None


async def test_concurrent_appends_get_distinct_seq(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The seq race resolves through IntegrityError + rollback + retry.

    On Postgres the losing INSERT aborts its transaction, so this only works
    because `MessageRepository.append` rolls back before retrying.
    """
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_A, CHANNEL)
    messages = SqlAlchemyMessageRepository(session_factory)

    await asyncio.gather(
        messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="one")),
        messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="two")),
    )

    stored = await messages.list(dialog.id)
    assert len(stored) == TWO_APPENDS
    async with session_factory() as session:
        seqs = sorted((await session.scalars(select(MessageRow.seq))).all())
    assert seqs == [1, 2]


async def test_cron_claim_is_won_once(session_factory: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAlchemyCronStore(session_factory)
    job = await store.create(cron_job("job-1", next_fire_at=NOW))
    stale_before = NOW - LEASE_STALE_AFTER

    first = await store.claim(job.id, job.next_fire_at, "owner-1", NOW, stale_before)
    second = await store.claim(job.id, job.next_fire_at, "owner-2", NOW, stale_before)

    assert first is True
    assert second is False  # the lease is taken; rowcount 0 on Postgres too


async def test_cron_claim_takes_over_a_stale_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyCronStore(session_factory)
    dead_claim = NOW - timedelta(hours=1)
    job = await store.create(cron_job("job-1", next_fire_at=NOW, claimed_at=dead_claim))

    claimed = await store.claim(job.id, job.next_fire_at, "owner-1", NOW, NOW - LEASE_STALE_AFTER)

    assert claimed is True


async def test_two_users_may_share_a_memory_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Memories live in instructions now: (type, title) may repeat across owners."""
    store = SqlAlchemyInstructionStore(session_factory)

    await store.upsert(memory_draft(SHARED_KEY, "a note", USER_A))
    await store.upsert(memory_draft(SHARED_KEY, "b note", USER_B))

    a_row = await store.get_by_title(SHARED_KEY, InstructionType.MEMORY, owner_id=USER_A)
    b_row = await store.get_by_title(SHARED_KEY, InstructionType.MEMORY, owner_id=USER_B)
    assert a_row is not None and a_row.content == "a note"
    assert b_row is not None and b_row.content == "b note"


async def test_missing_embeddings_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """json_array_length must find empty-embedding rows on Postgres too."""
    store = SqlAlchemyInstructionStore(session_factory)
    saved = await store.upsert(memory_draft(SHARED_KEY, GLOBAL_CONTENT, USER_A, embedding=()))

    pending = await store.list_missing_embeddings()
    stored = await store.set_embedding(saved.id, (1.0, 0.0))

    assert [record.id for record in pending] == [saved.id]
    assert stored is True
    assert await store.list_missing_embeddings() == []


async def test_private_instruction_shadows_the_public_one(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """(type, title) may repeat across owners; only public pairs are unique."""
    store = SqlAlchemyInstructionStore(session_factory)

    public = await store.upsert(draft(None))
    private = await store.upsert(draft(OWNER))

    assert public.id != private.id
    assert public.owner_id is None
    assert private.owner_id == OWNER


async def test_instruction_upsert_survives_a_concurrent_insert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The find-then-insert race ends as an update of the winner's row."""
    store = SqlAlchemyInstructionStore(session_factory)

    await asyncio.gather(store.upsert(draft(None)), store.upsert(draft(None)))

    records = await store.list_with_embeddings(None)
    versions = [item.instruction.version for item in records]
    assert len(records) == 1
    assert versions == [SECOND_VERSION]


async def test_dataset_records_filter_by_date_range(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyDatasetStore(session_factory)
    dataset = await store.create(OWNER, "meals", "meal log", DatasetSchema(()), "", "", EMBEDDING)
    await store.add_record(dataset.id, OWNER, {"dish": "soup"})

    inside = await store.query_candidates(dataset.id, NOW - timedelta(days=1), None, SCAN_LIMIT)
    outside = await store.query_candidates(dataset.id, None, NOW - timedelta(days=1), SCAN_LIMIT)

    assert len(inside) == 1
    assert outside == []
