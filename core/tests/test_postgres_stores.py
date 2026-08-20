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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_core.composition import build_collections
from octoforge_core.context.pg_store import PostgresSummaryStore
from octoforge_core.cron.api import CronJob
from octoforge_core.cron.store import SqlAlchemyCronStore
from octoforge_core.datasets.api import DatasetSchema
from octoforge_core.datasets.pg_store import PostgresDatasetStore
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.db.engine import bootstrap_schema, create_engine, create_session_factory
from octoforge_core.db.search_extensions import (
    UNACCENT,
    VECTOR,
    has_russian_unaccent,
    installed_search_extensions,
)
from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.dialogs.store import (
    SqlAlchemyDialogRepository,
    SqlAlchemyExchangeRepository,
    SqlAlchemyMessageRepository,
)
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.identity.api import UserStatus
from octoforge_core.identity.store import SqlAlchemyIdentityStore
from octoforge_core.instructions.api import (
    InstructionDraft,
    InstructionLexicalSearch,
    InstructionType,
    InstructionVectorSearch,
)
from octoforge_core.instructions.pg_store import PostgresInstructionStore
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionKind,
    CollectionNotFoundError,
    CollectionQueryError,
    FilterOp,
    FilterPredicate,
    JoinSpec,
    NewRecords,
    Query,
    QueryOp,
)
from octoforge_core.net.collections.engine import PostgresCollectionQueryEngine
from octoforge_core.net.collections.schema_infer import infer_records
from octoforge_core.net.collections.store import SqlAlchemyCollectionStore
from octoforge_core.settings.api import max_active_users
from octoforge_core.settings.store import SqlAlchemySettingsStore
from octoforge_core.tariffs.api import (
    FeatureCode,
    TariffLimits,
    UsageEvent,
    UsageKind,
    UsageOrigin,
)
from octoforge_core.tariffs.store import SqlAlchemyTariffStore, SqlAlchemyUsageMeter
from octoforge_core.time import utc_now

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
# spelled without the diaeresis, and a nominative singular against an
# inflected plural in the document
RUSSIAN_QUERY_VARIANTS = ("ежик", "объем", "задача")
TWO_APPENDS = 2
SECOND_VERSION = 2
SEARCH_LIMIT = 10
EMBEDDING_MODEL = "test-embedder"
WIDER_DIMENSION = 8
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


async def test_bootstrap_installs_the_optional_search_extensions(
    pristine_engine: AsyncEngine,
) -> None:
    """A fresh database must not miss pgvector and BM25 by taking the create_all path.

    `_create_and_stamp` skips the migration chain outside SQLite, so the
    extensions have to be created by that branch too. The regression this pins
    is silent: search would simply stay on brute-force cosine forever.
    """
    await bootstrap_schema(pristine_engine)

    async with pristine_engine.connect() as connection:
        installed = await connection.run_sync(installed_search_extensions)
        russian = await connection.run_sync(has_russian_unaccent)

    assert VECTOR in installed, "pgvector is expected in the image this suite runs against"
    assert UNACCENT in installed
    assert russian is True


async def test_russian_lexical_config_folds_case_accents_and_inflection(
    engine: AsyncEngine,
) -> None:
    """`russian_unaccent` must match across the diaeresis and across inflected forms.

    This is the whole reason for a custom configuration: the stock `russian`
    config treats ё as a letter of its own, so a query spelled without the
    diaeresis would not find a record that has it. Asserted through `to_tsquery`
    rather than a BM25 index, so it also covers deployments without pg_textsearch.
    """
    document = "Ёжик собирает объём отложенных задач"
    async with engine.connect() as connection:
        for query in RUSSIAN_QUERY_VARIANTS:
            matched = await connection.scalar(
                text(
                    "SELECT to_tsvector('public.russian_unaccent', :document) "
                    "@@ to_tsquery('public.russian_unaccent', :query)"
                ),
                {"document": document, "query": query},
            )
            assert matched is True, f"{query!r} should have matched {document!r}"

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


async def test_user_activity_aggregate_comes_back_as_aware_utc(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The `max(case(...))` aggregate must survive this dialect too: asyncpg
    hands the timestamptz back aware, `_as_utc` only normalizes it."""
    dialog = await SqlAlchemyDialogRepository(session_factory).get_or_create(USER_A, CHANNEL)
    messages = SqlAlchemyMessageRepository(session_factory)
    await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="hi"))
    await messages.append(dialog.id, ChatMessage(role=MessageRole.ASSISTANT, content="reply"))

    activity = await messages.user_activity_by_channel(CHANNEL, utc_now() - timedelta(hours=24))

    (entry,) = activity
    assert entry.user_messages_since == 1  # the agent's reply is not the user's writing
    assert entry.last_user_message_at is not None
    assert entry.last_user_message_at.utcoffset() == timedelta(0)


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


async def test_unit_of_work_savepoint_spares_earlier_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The append-retry SAVEPOINT branch is PostgreSQL-only, so it is proven here.

    On Postgres a failed statement aborts the whole transaction; without the
    savepoint the violation below would poison the unit and take the created
    exchange with it. (SQLite takes the other branch: pysqlite's savepoints
    implicitly commit, and a failed statement does not abort there anyway.)
    """
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    uow = UnitOfWork(session_factory)
    dialog = await dialogs.get_or_create(USER_A, CHANNEL)
    original = ChatMessage(role=MessageRole.USER, content="hi")
    await messages.append(dialog.id, original, client_message_id="uow-dup")

    async with uow():
        exchange = await exchanges.create(dialog.id, "question")
        with pytest.raises(IntegrityError):  # the idempotency key is taken
            await messages.append(dialog.id, original, client_message_id="uow-dup")
        # the violation cost the attempt alone: the unit is still usable
        await exchanges.set_title(exchange.id, "still alive")
        await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="again"))

    assert (await exchanges.get(exchange.id)).title == "still alive"
    assert [message.content for message in await messages.list(dialog.id)] == ["hi", "again"]


async def test_unit_of_work_first_append_survives_its_violation_bare(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """With no earlier writes in the unit, the failed attempt skips the
    SAVEPOINT and rolls the still-empty transaction back — the unit stays
    usable and commits everything that comes after."""
    dialogs = SqlAlchemyDialogRepository(session_factory)
    messages = SqlAlchemyMessageRepository(session_factory)
    exchanges = SqlAlchemyExchangeRepository(session_factory)
    uow = UnitOfWork(session_factory)
    dialog = await dialogs.get_or_create(USER_A, CHANNEL)
    original = ChatMessage(role=MessageRole.USER, content="hi")
    await messages.append(dialog.id, original, client_message_id="uow-dup")

    async with uow():
        with pytest.raises(IntegrityError):  # first call of the unit, key taken
            await messages.append(dialog.id, original, client_message_id="uow-dup")
        await messages.append(dialog.id, ChatMessage(role=MessageRole.USER, content="again"))
        exchange = await exchanges.create(dialog.id, "question")

    assert [message.content for message in await messages.list(dialog.id)] == ["hi", "again"]
    assert (await exchanges.get(exchange.id)).status is ExchangeStatus.OPEN


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


async def test_stale_embeddings_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The stale-vector query must work on Postgres, empty JSON arrays included.

    Also covers writing the pgvector column: `set_embedding` stores the same
    numbers twice, and a `vector` bind that asyncpg cannot adapt would only
    ever show up here.
    """
    store = SqlAlchemyInstructionStore(session_factory)
    saved = await store.upsert(memory_draft(SHARED_KEY, GLOBAL_CONTENT, USER_A, embedding=()))

    pending = await store.list_stale_embeddings(EMBEDDING_MODEL, limit=SEARCH_LIMIT)
    stored = await store.set_embedding(saved.id, (1.0, 0.0), EMBEDDING_MODEL)

    assert [record.id for record in pending] == [saved.id]
    assert stored is True
    assert await store.list_stale_embeddings(EMBEDDING_MODEL, limit=SEARCH_LIMIT) == []


async def test_a_changed_model_makes_every_row_stale(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The whole point of the model column: a swap is visible, not silent."""
    store = SqlAlchemyInstructionStore(session_factory)
    saved = await store.upsert(memory_draft(SHARED_KEY, GLOBAL_CONTENT, USER_A))
    await store.set_embedding(saved.id, (1.0, 0.0), EMBEDDING_MODEL)

    settled = await store.list_stale_embeddings(EMBEDDING_MODEL, limit=SEARCH_LIMIT)
    after_swap = await store.list_stale_embeddings("another-model", limit=SEARCH_LIMIT)

    assert settled == []
    assert [record.id for record in after_swap] == [saved.id]


async def test_the_vector_column_takes_whatever_dimension_the_model_produces(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No dimension in the schema, so two models' vectors can coexist mid-swap.

    `vector(1024)` would have rejected the second write outright, which is
    exactly the coupling this column is declared unsized to avoid.
    """
    store = SqlAlchemyInstructionStore(session_factory)
    small = await store.upsert(memory_draft("small", GLOBAL_CONTENT, USER_A))
    large = await store.upsert(memory_draft("large", GLOBAL_CONTENT, USER_B))

    assert await store.set_embedding(small.id, (1.0, 0.0), EMBEDDING_MODEL) is True
    assert await store.set_embedding(large.id, tuple([0.5] * WIDER_DIMENSION), "wider") is True


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


async def test_vector_search_returns_the_nearest_records_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """pgvector must order by cosine distance, not by insertion or id."""
    store = PostgresInstructionStore(session_factory)
    near = await store.upsert(memory_draft("near", GLOBAL_CONTENT, USER_A, embedding=(1.0, 0.0)))
    far = await store.upsert(memory_draft("far", GLOBAL_CONTENT, USER_A, embedding=(-1.0, 0.0)))
    middle = await store.upsert(
        memory_draft("middle", GLOBAL_CONTENT, USER_A, embedding=(0.7, 0.7))
    )

    hits = await store.search_by_vector((1.0, 0.0), limit=SEARCH_LIMIT, user_id=USER_A)

    assert [hit.instruction.id for hit in hits] == [near.id, middle.id, far.id]


async def test_vector_search_honours_visibility(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Another user's private record must never reach the ranking input.

    Ownership is a SQL predicate everywhere else; moving the search into the
    database must not quietly drop it.
    """
    store = PostgresInstructionStore(session_factory)
    mine = await store.upsert(memory_draft("mine", GLOBAL_CONTENT, USER_A, embedding=(1.0, 0.0)))
    public = await store.upsert(memory_draft("public", GLOBAL_CONTENT, None, embedding=(1.0, 0.0)))
    await store.upsert(memory_draft("theirs", GLOBAL_CONTENT, USER_B, embedding=(1.0, 0.0)))

    hits = await store.search_by_vector((1.0, 0.0), limit=SEARCH_LIMIT, user_id=USER_A)

    assert {hit.instruction.id for hit in hits} == {mine.id, public.id}


async def test_vector_search_skips_records_of_another_dimension(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A half-absorbed model change must degrade, not raise.

    Postgres refuses to compare vectors of different sizes ("different vector
    dimensions"), so without the vector_dims filter a single leftover record
    from the previous model would break every recall in the installation until
    the sweep caught up.
    """
    store = PostgresInstructionStore(session_factory)
    current = await store.upsert(
        memory_draft("current", GLOBAL_CONTENT, USER_A, embedding=(1.0, 0.0))
    )
    await store.upsert(
        memory_draft("previous", GLOBAL_CONTENT, USER_A, embedding=tuple([0.1] * WIDER_DIMENSION))
    )

    hits = await store.search_by_vector((1.0, 0.0), limit=SEARCH_LIMIT, user_id=USER_A)

    assert [hit.instruction.id for hit in hits] == [current.id]


async def test_vector_search_skips_records_with_no_vector(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A record whose embedding failed is simply not a candidate."""
    store = PostgresInstructionStore(session_factory)
    embedded = await store.upsert(
        memory_draft("embedded", GLOBAL_CONTENT, USER_A, embedding=(1.0, 0.0))
    )
    await store.upsert(memory_draft("deferred", GLOBAL_CONTENT, USER_A, embedding=()))

    hits = await store.search_by_vector((1.0, 0.0), limit=SEARCH_LIMIT, user_id=USER_A)

    assert [hit.instruction.id for hit in hits] == [embedded.id]


async def test_vector_search_is_the_capability_the_service_detects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The portable store must NOT claim the capability, or a database without
    pgvector would fail at the first recall instead of at startup."""
    assert isinstance(PostgresInstructionStore(session_factory), InstructionVectorSearch)
    assert not isinstance(SqlAlchemyInstructionStore(session_factory), InstructionVectorSearch)


def text_draft(title: str, content: str, owner_id: str | None) -> InstructionDraft:
    return InstructionDraft(
        kind=InstructionType.KNOWLEDGE,
        title=title,
        content=content,
        tags=(),
        embedding=EMBEDDING,
        owner_id=owner_id,
    )


async def test_lexical_search_finds_the_exact_term_an_embedding_would_miss(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason BM25 is here at all: one literal string, not a topic."""
    store = PostgresInstructionStore(session_factory)
    wanted = await store.upsert(
        text_draft("billing", "При ошибке E_INVOICE_4021 повторите запрос позже", USER_A)
    )
    await store.upsert(text_draft("shipping", "Расчёт стоимости доставки по регионам", USER_A))

    hits = await store.search_by_text("E_INVOICE_4021", limit=SEARCH_LIMIT, user_id=USER_A)

    assert [hit.instruction.id for hit in hits] == [wanted.id]


async def test_lexical_search_matches_across_russian_inflection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """russian_unaccent must stem, or Russian keyword search is close to useless."""
    store = PostgresInstructionStore(session_factory)
    wanted = await store.upsert(
        text_draft("tasks", "Агент создаёт отложенные задачи и напоминания", USER_A)
    )

    hits = await store.search_by_text("задача", limit=SEARCH_LIMIT, user_id=USER_A)

    assert [hit.instruction.id for hit in hits] == [wanted.id]


async def test_lexical_search_honours_visibility_and_kind(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ownership is a SQL predicate everywhere; BM25 must not be the exception.

    The kind filter has to live in the query too: `limit` is spent before the
    caller could filter, so a top-N of one type would starve a search for
    another.
    """
    store = PostgresInstructionStore(session_factory)
    mine = await store.upsert(text_draft("mine", "уникальное слово корвалол", USER_A))
    await store.upsert(text_draft("theirs", "уникальное слово корвалол", USER_B))

    visible = await store.search_by_text("корвалол", limit=SEARCH_LIMIT, user_id=USER_A)
    wrong_kind = await store.search_by_text(
        "корвалол", limit=SEARCH_LIMIT, user_id=USER_A, kinds=(InstructionType.ENDPOINT,)
    )

    assert [hit.instruction.id for hit in visible] == [mine.id]
    assert wrong_kind == []


async def test_lexical_search_returns_nothing_when_no_word_matches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BM25 is a filter as much as an ordering: a non-match must not be ranked last."""
    store = PostgresInstructionStore(session_factory)
    await store.upsert(text_draft("shipping", "Расчёт стоимости доставки", USER_A))

    hits = await store.search_by_text("квазистационарный", limit=SEARCH_LIMIT, user_id=USER_A)

    assert hits == []


async def test_the_title_is_searched_as_a_document_of_its_own(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two BM25 indexes, not one over title||content.

    A record whose term appears ONLY in its title has to come back. A single
    index over the body would never see it, and one over the concatenation
    would bury it: BM25 normalizes by document length, so a term in a
    two-token title carries real weight while the same term folded into a
    two-hundred-token body barely registers. Both records here must surface,
    each found by a different index.
    """
    store = PostgresInstructionStore(session_factory)
    title_only = await store.upsert(text_draft("корвалол", "нечто совершенно другое", USER_A))
    body_only = await store.upsert(
        text_draft("prose", " ".join(["наполнитель"] * 200) + " корвалол", USER_A)
    )

    hits = await store.search_by_text("корвалол", limit=SEARCH_LIMIT, user_id=USER_A)

    assert {hit.instruction.id for hit in hits} == {title_only.id, body_only.id}


async def test_lexical_search_is_the_capability_the_service_detects(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only the Postgres store may claim it; the portable one must not."""
    assert isinstance(PostgresInstructionStore(session_factory), InstructionLexicalSearch)
    assert not isinstance(SqlAlchemyInstructionStore(session_factory), InstructionLexicalSearch)


async def _dialog_with_messages(
    session_factory: async_sessionmaker[AsyncSession],
    bodies: tuple[str, ...],
) -> str:
    """Insert one dialog and its messages; return the dialog id."""
    dialog_id = "dialog-history"
    async with session_factory() as session:
        session.add(DialogRow(id=dialog_id, user_id=USER_A, channel=CHANNEL))
        for seq, body in enumerate(bodies, start=1):
            session.add(
                MessageRow(
                    id=f"m{seq}",
                    dialog_id=dialog_id,
                    seq=seq,
                    role=MessageRole.USER.value,
                    content=body,
                )
            )
        await session.commit()
    return dialog_id


async def test_history_search_matches_across_russian_inflection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reason BM25 replaced ILIKE here.

    The old implementation matched `content ILIKE '%query%'`, so asking about
    "задача" could not find a message that said "задачи" — which is most of
    them, in a language with cases. The user had to guess the exact inflected
    form somebody typed months ago.
    """
    dialog_id = await _dialog_with_messages(
        session_factory,
        ("Поставь мне несколько задач на завтра", "Погода на выходных обещает дождь"),
    )
    store = PostgresSummaryStore(session_factory)

    hits = await store.search(dialog_id, "задача", limit=SEARCH_LIMIT)

    assert [hit.seq for hit in hits] == [1]


async def test_history_search_ranks_by_relevance_not_by_position(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A passing mention must not outrank the message actually about the topic
    merely by having been said first."""
    dialog_id = await _dialog_with_messages(
        session_factory,
        ("Кстати про корвалол вскользь", "Корвалол, корвалол и ещё раз корвалол"),
    )
    store = PostgresSummaryStore(session_factory)

    hits = await store.search(dialog_id, "корвалол", limit=SEARCH_LIMIT)

    assert [hit.seq for hit in hits] == [2, 1]


async def test_history_search_stays_inside_its_dialog(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ranking moved into SQL; the dialog predicate must move with it."""
    dialog_id = await _dialog_with_messages(session_factory, ("общий уникальный термин",))
    async with session_factory() as session:
        session.add(DialogRow(id="other-dialog", user_id=USER_B, channel=CHANNEL))
        session.add(
            MessageRow(
                id="m-other",
                dialog_id="other-dialog",
                seq=1,
                role=MessageRole.USER.value,
                content="общий уникальный термин",
            )
        )
        await session.commit()
    store = PostgresSummaryStore(session_factory)

    hits = await store.search(dialog_id, "термин", limit=SEARCH_LIMIT)

    assert len(hits) == 1


async def test_history_search_returns_nothing_when_no_word_matches(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dialog_id = await _dialog_with_messages(session_factory, ("совсем про другое",))
    store = PostgresSummaryStore(session_factory)

    assert await store.search(dialog_id, "квазистационарный", limit=SEARCH_LIMIT) == []


async def test_dataset_lexical_search_finds_a_word_the_embedding_would_not(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Small win, but a real one: an abbreviation nowhere near the query in
    vector space still names its dataset."""
    store = PostgresDatasetStore(session_factory)
    wanted = await store.create(
        owner_user_id=USER_A,
        name="meals",
        description="Дневник питания и расчёт КБЖУ по каждому приёму пищи",
        schema=DatasetSchema(fields=()),
        usage_notes="",
        retention="",
        embedding=EMBEDDING,
    )
    await store.create(
        owner_user_id=USER_A,
        name="expenses",
        description="Учёт трат по категориям",
        schema=DatasetSchema(fields=()),
        usage_notes="",
        retention="",
        embedding=EMBEDDING,
    )

    hits = await store.search_by_text(USER_A, "КБЖУ", limit=SEARCH_LIMIT)

    assert [hit.dataset.id for hit in hits] == [wanted.id]


async def test_dataset_lexical_search_is_owner_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Owner isolation is the store's duty on every path, this one included."""
    store = PostgresDatasetStore(session_factory)
    await store.create(
        owner_user_id=USER_B,
        name="theirs",
        description="Дневник питания и расчёт КБЖУ",
        schema=DatasetSchema(fields=()),
        usage_notes="",
        retention="",
        embedding=EMBEDDING,
    )

    assert await store.search_by_text(USER_A, "КБЖУ", limit=SEARCH_LIMIT) == []


async def test_tariff_roundtrip_and_assignment(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token_limit = 1000
    store = SqlAlchemyTariffStore(session_factory)
    await store.put(
        "basic",
        "Basic",
        frozenset({FeatureCode.WEB_SEARCH}),
        TariffLimits(daily_tokens=token_limit),
    )

    await store.assign(USER_A, "basic")
    bound = await store.tariff_for_user(USER_A)

    assert bound is not None
    assert bound.features == {FeatureCode.WEB_SEARCH}
    assert bound.limits.daily_tokens == token_limit
    assert await store.tariff_for_user(USER_B) is None


async def test_usage_ledger_concurrent_appends_all_land(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two nodes meter the same user concurrently; insert-only must lose nothing."""
    writers = 10
    meter = SqlAlchemyUsageMeter(session_factory)
    since = utc_now() - timedelta(minutes=1)
    event = UsageEvent(
        user_id=USER_A,
        kind=UsageKind.LLM_ANSWER,
        origin=UsageOrigin.INTERACTIVE,
        prompt_tokens=10,
        completion_tokens=5,
        quantity=1,
    )

    await asyncio.gather(*(meter.record(event) for _ in range(writers)))
    totals = await meter.totals_since(USER_A, since)

    assert totals.assistant_messages == writers
    assert totals.tokens == writers * (event.prompt_tokens + event.completion_tokens)


async def test_concurrent_activation_respects_the_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Many contenders, one slot: the conditional UPDATE is the referee.

    This is the cross-process guarantee the SQLite suite cannot give — on
    Postgres the head-count subquery and the status flip run as one
    statement under real concurrency.
    """
    contenders = 8
    cap = 1
    identity = SqlAlchemyIdentityStore(session_factory)
    people = [await identity.resolve_or_create("telegram", f"acct-{n}") for n in range(contenders)]

    outcomes = await asyncio.gather(*(identity.try_activate(person, cap) for person in people))
    counts = await identity.count_by_status()

    assert sum(outcomes) == cap  # not one over, not one under
    assert counts[UserStatus.ACTIVE] == cap
    assert counts[UserStatus.WAITING] == contenders - cap


async def test_settings_roundtrip(session_factory: async_sessionmaker[AsyncSession]) -> None:
    cap = 3
    store = SqlAlchemySettingsStore(session_factory)

    await store.put("max_active_users", str(cap))
    assert await max_active_users(store) == cap

    await store.delete("max_active_users")
    assert await max_active_users(store) is None


# --- collections: the jsonb query engine --------------------------------------
#
# The engine compiles the op-DSL into `payload #>> path` SQL and exists ONLY on
# Postgres — which is precisely why its behavior is asserted here and nowhere
# else: SQLite cannot even parse these statements.

CONTRACTORS = [
    {"id": 1, "name": "Alpha", "amount": 100.0, "status": "active", "region": "north"},
    {"id": 2, "name": "Beta", "amount": 250.5, "status": "active", "region": "south"},
    {"id": 3, "name": "Gamma", "amount": 50.0, "status": "closed", "region": "north"},
    {"id": 4, "name": "Delta", "amount": 300.0, "status": "active", "region": "north"},
]
ACTIVE_TOTAL = 650.5
NORTH_TOTAL = 450.0
COLLECTION_OWNER = "person-collections"


async def _contractor_collection(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[SqlAlchemyCollectionStore, str]:
    store = SqlAlchemyCollectionStore(session_factory)
    passport = await store.create(
        owner_id=COLLECTION_OWNER,
        label="contractors",
        kind=CollectionKind.JSON,
        source="endpoint:crm",
        schema=infer_records(CONTRACTORS),
        envelope={},
        records=NewRecords(payloads=CONTRACTORS, source="endpoint:crm"),
        byte_size=1000,
        truncated=False,
        expires_at=utc_now() + timedelta(hours=1),
    )
    return store, passport.id


async def test_collections_sum_filter_and_group_by(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The contractor scenario: aggregate in the database, not in context."""
    _, ref = await _contractor_collection(session_factory)
    engine_ = PostgresCollectionQueryEngine(session_factory)

    total = await engine_.execute(
        COLLECTION_OWNER,
        ref,
        Query(
            op=QueryOp.SUM,
            field="amount",
            filters=(FilterPredicate(field="status", op=FilterOp.EQ, value="active"),),
        ),
    )
    grouped = await engine_.execute(
        COLLECTION_OWNER, ref, Query(op=QueryOp.SUM, field="amount", group_by="region")
    )

    assert total.rows == [ACTIVE_TOTAL]
    by_region = {row["group"]: row["value"] for row in grouped.rows}
    assert by_region["north"] == NORTH_TOTAL
    assert grouped.total == len(by_region)


async def test_collections_pluck_pages_in_element_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, ref = await _contractor_collection(session_factory)
    engine_ = PostgresCollectionQueryEngine(session_factory)

    page = await engine_.execute(
        COLLECTION_OWNER, ref, Query(op=QueryOp.PLUCK, field="name", limit=2, offset=1)
    )

    assert page.rows == ["Beta", "Gamma"]
    assert page.total == len(CONTRACTORS)


async def test_collections_numeric_filter_compares_as_numbers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """'90' > '250.5' as text; the schema's number type must pick the cast."""
    _, ref = await _contractor_collection(session_factory)
    engine_ = PostgresCollectionQueryEngine(session_factory)

    result = await engine_.execute(
        COLLECTION_OWNER,
        ref,
        Query(
            op=QueryOp.COUNT,
            filters=(FilterPredicate(field="amount", op=FilterOp.GT, value=90),),
        ),
    )

    assert result.rows == [3]


async def test_collections_get_and_distinct(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, ref = await _contractor_collection(session_factory)
    engine_ = PostgresCollectionQueryEngine(session_factory)

    records = await engine_.execute(COLLECTION_OWNER, ref, Query(op=QueryOp.GET, limit=1))
    statuses = await engine_.execute(
        COLLECTION_OWNER, ref, Query(op=QueryOp.DISTINCT, field="status")
    )

    assert records.rows == [CONTRACTORS[0]]
    assert sorted(statuses.rows) == ["active", "closed"]


async def test_collections_unknown_field_lists_the_real_ones(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The self-correction contract: the error carries what does exist."""
    _, ref = await _contractor_collection(session_factory)
    engine_ = PostgresCollectionQueryEngine(session_factory)

    with pytest.raises(CollectionQueryError) as failure:
        await engine_.execute(COLLECTION_OWNER, ref, Query(op=QueryOp.PLUCK, field="price"))

    assert "amount" in str(failure.value)


async def test_collections_sum_over_a_string_field_is_refused_with_the_remedy(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, ref = await _contractor_collection(session_factory)
    engine_ = PostgresCollectionQueryEngine(session_factory)

    with pytest.raises(CollectionQueryError) as failure:
        await engine_.execute(COLLECTION_OWNER, ref, Query(op=QueryOp.SUM, field="name"))

    assert "response section" in str(failure.value)


async def test_collections_are_walled_per_owner_and_expiry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, ref = await _contractor_collection(session_factory)
    engine_ = PostgresCollectionQueryEngine(session_factory)

    with pytest.raises(CollectionNotFoundError):
        await engine_.execute("somebody-else", ref, Query(op=QueryOp.COUNT))


async def test_collections_source_tag_narrows_a_merged_collection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Records appended from another endpoint are queryable apart (the join stub)."""
    store, ref = await _contractor_collection(session_factory)
    contacts = [{"contractor_id": 1, "email": "a@x"}, {"contractor_id": 2, "email": "b@x"}]
    merged_schema = infer_records(CONTRACTORS + contacts)
    await store.append(
        COLLECTION_OWNER,
        ref,
        NewRecords(payloads=contacts, source="endpoint:contacts"),
        schema=merged_schema,
        byte_size=100,
        expires_at=utc_now() + timedelta(hours=1),
    )
    engine_ = PostgresCollectionQueryEngine(session_factory)

    contacts_only = await engine_.execute(
        COLLECTION_OWNER, ref, Query(op=QueryOp.COUNT, source="endpoint:contacts")
    )
    everything = await engine_.execute(COLLECTION_OWNER, ref, Query(op=QueryOp.COUNT))

    assert contacts_only.rows == [2]
    assert everything.rows == [len(CONTRACTORS) + 2]


async def test_collections_dotted_paths_reach_nested_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAlchemyCollectionStore(session_factory)
    nested = [{"owner": {"city": "SPb"}}, {"owner": {"city": "Kazan"}}, {"owner": {"city": "SPb"}}]
    passport = await store.create(
        owner_id=COLLECTION_OWNER,
        label="",
        kind=CollectionKind.JSON,
        source="endpoint:crm",
        schema=infer_records(nested),
        envelope={},
        records=NewRecords(payloads=nested),
        byte_size=100,
        truncated=False,
        expires_at=utc_now() + timedelta(hours=1),
    )
    engine_ = PostgresCollectionQueryEngine(session_factory)

    grouped = await engine_.execute(
        COLLECTION_OWNER, passport.id, Query(op=QueryOp.COUNT, group_by="owner.city")
    )

    assert {row["group"]: row["value"] for row in grouped.rows} == {"SPb": 2, "Kazan": 1}


async def test_collections_feature_is_built_on_postgres(
    engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The composition-root capability check: here, and only here, it answers."""
    runtime = build_collections(engine, session_factory, CollectionConfig())
    assert runtime is not None
    assert runtime.query_tool.spec.name == "collection_query"


CONTACTS = [
    {"contractor_id": 1, "email": "alpha@x"},
    {"contractor_id": 1, "email": "alpha2@x"},
    {"contractor_id": 3, "email": "gamma@x"},
]


async def test_collections_join_pairs_records_across_collections(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Contractors from one endpoint, contacts from another: the join runs in
    SQL and answers pairs — never a megabyte of both through the context."""
    store, left_ref = await _contractor_collection(session_factory)
    right = await store.create(
        owner_id=COLLECTION_OWNER,
        label="contacts",
        kind=CollectionKind.JSON,
        source="endpoint:contacts",
        schema=infer_records(CONTACTS),
        envelope={},
        records=NewRecords(payloads=CONTACTS, source="endpoint:contacts"),
        byte_size=300,
        truncated=False,
        expires_at=utc_now() + timedelta(hours=1),
    )
    engine_ = PostgresCollectionQueryEngine(session_factory)

    pairs = await engine_.execute(
        COLLECTION_OWNER,
        left_ref,
        Query(
            op=QueryOp.GET,
            join=JoinSpec(ref=right.id, on_left="id", on_right="contractor_id"),
            filters=(FilterPredicate(field="status", op=FilterOp.EQ, value="active"),),
        ),
    )
    counted = await engine_.execute(
        COLLECTION_OWNER,
        left_ref,
        Query(
            op=QueryOp.COUNT, join=JoinSpec(ref=right.id, on_left="id", on_right="contractor_id")
        ),
    )

    # Alpha (active, id=1) matches two contacts; Gamma is filtered out (closed)
    assert [(row["left"]["name"], row["right"]["email"]) for row in pairs.rows] == [
        ("Alpha", "alpha@x"),
        ("Alpha", "alpha2@x"),
    ]
    assert pairs.total == len(pairs.rows)
    assert counted.rows == [len(CONTACTS)]  # unfiltered: both Alpha contacts plus Gamma's


async def test_collections_join_validates_both_sides(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, left_ref = await _contractor_collection(session_factory)
    right = await store.create(
        owner_id=COLLECTION_OWNER,
        label="",
        kind=CollectionKind.JSON,
        source="endpoint:contacts",
        schema=infer_records(CONTACTS),
        envelope={},
        records=NewRecords(payloads=CONTACTS),
        byte_size=300,
        truncated=False,
        expires_at=utc_now() + timedelta(hours=1),
    )
    engine_ = PostgresCollectionQueryEngine(session_factory)

    with pytest.raises(CollectionQueryError, match="contractor_id"):
        await engine_.execute(
            COLLECTION_OWNER,
            left_ref,
            Query(op=QueryOp.GET, join=JoinSpec(ref=right.id, on_left="id", on_right="wrong")),
        )
    with pytest.raises(CollectionQueryError, match="join combines only"):
        await engine_.execute(
            COLLECTION_OWNER,
            left_ref,
            Query(
                op=QueryOp.SUM,
                field="amount",
                join=JoinSpec(ref=right.id, on_left="id", on_right="contractor_id"),
            ),
        )
    with pytest.raises(CollectionNotFoundError):
        await engine_.execute(
            COLLECTION_OWNER,
            left_ref,
            Query(op=QueryOp.GET, join=JoinSpec(ref="ghost", on_left="id", on_right="x")),
        )


async def test_collections_concurrent_appends_do_not_lose_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two appends to the same collection race; the advisory lock must let
    both land with distinct positions, not one clobber the other (bug E)."""
    store = SqlAlchemyCollectionStore(session_factory)
    passport = await store.create(
        owner_id=COLLECTION_OWNER,
        label="",
        kind=CollectionKind.JSON,
        source="endpoint:a",
        schema=infer_records([{"id": 0}]),
        envelope={},
        records=NewRecords(payloads=[{"id": 0}]),
        byte_size=100,
        truncated=False,
        expires_at=utc_now() + timedelta(hours=1),
    )

    async def append_one(marker: int) -> None:
        await store.append(
            COLLECTION_OWNER,
            passport.id,
            NewRecords(payloads=[{"id": marker}], source=f"src-{marker}"),
            schema=infer_records([{"id": marker}]),
            byte_size=100,
            expires_at=utc_now() + timedelta(hours=1),
        )

    await asyncio.gather(*(append_one(n) for n in range(1, 6)))

    final = await store.passport(COLLECTION_OWNER, passport.id)
    expected_total = 6  # the seed plus all five appends, none lost
    assert final.record_count == expected_total
    engine_ = PostgresCollectionQueryEngine(session_factory)
    ids = await engine_.execute(
        COLLECTION_OWNER, passport.id, Query(op=QueryOp.PLUCK, field="id", limit=100)
    )
    assert sorted(ids.rows) == [0, 1, 2, 3, 4, 5]  # every record present, once
