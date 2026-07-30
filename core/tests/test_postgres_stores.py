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
from octoforge_core.dialogs.models import DialogRow, MessageRow
from octoforge_core.dialogs.store import SqlAlchemyDialogRepository, SqlAlchemyMessageRepository
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.instructions.api import (
    InstructionDraft,
    InstructionLexicalSearch,
    InstructionType,
    InstructionVectorSearch,
)
from octoforge_core.instructions.pg_store import PostgresInstructionStore
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
