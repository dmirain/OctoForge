"""Contract-style tests for InstructionService implementations.

The suite talks to the facade only (search/save/get_by_name) and builds the
service under test through the `service_factory` fixture, so the same suite
can later validate an HTTP implementation of the protocol by swapping that
one fixture.
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.instructions.api import (
    InstructionNotFoundError,
    InstructionService,
    InstructionType,
    SystemInstructionError,
)
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.llm.embeddings import EmbeddingClient

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
EMBEDDED_TEXT_SEPARATOR = "\n"
VERSION_CREATED = 1
VERSION_REPLACED = 2
USAGE_NEVER = 0
USAGE_ONCE = 1
USAGE_TWICE = 2
TWO_HITS = 2
THREE_HITS = 3
TWO_EMBED_CALLS = 2

V_RIGHT = (1.0, 0.0)
V_UP = (0.0, 1.0)
V_DIAGONAL = (0.6, 0.8)

TITLE_ALPHA = "alpha fact"
TITLE_BETA = "beta fact"
CONTENT_A = "content A"
CONTENT_B = "content B"
QUERY = "find something"
EXACT_QUERY = "ALPHA FACT"  # matches TITLE_ALPHA case-insensitively


class StubEmbedder:
    """Deterministic EmbeddingClient: exact text-to-vector mapping."""

    def __init__(self) -> None:
        self.vectors: dict[str, tuple[float, ...]] = {}
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple(self.vectors[text] for text in texts)


ServiceFactory = Callable[[async_sessionmaker[AsyncSession], EmbeddingClient], InstructionService]


def build_local_service(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingClient,
) -> InstructionService:
    """Assemble the default local implementation over the SQL store."""
    return LocalInstructionService(SqlAlchemyInstructionStore(session_factory), embedder)


@pytest.fixture
def service_factory() -> ServiceFactory:
    """The implementation under test; swap to run the suite over another one."""
    return build_local_service


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def embedder() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def service(
    service_factory: ServiceFactory,
    session_factory: async_sessionmaker[AsyncSession],
    embedder: StubEmbedder,
) -> InstructionService:
    return service_factory(session_factory, embedder)


def register_vector(
    embedder: StubEmbedder,
    title: str,
    content: str,
    vector: tuple[float, ...],
) -> None:
    """Map the exact text the service embeds for a record to a vector."""
    embedder.vectors[f"{title}{EMBEDDED_TEXT_SEPARATOR}{content}"] = vector


async def test_save_creates_record(service: InstructionService, embedder: StubEmbedder) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)

    saved = await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A, ("api", "facts"))

    assert saved.id
    assert saved.type is InstructionType.KNOWLEDGE
    assert saved.title == TITLE_ALPHA
    assert saved.content == CONTENT_A
    assert saved.tags == ("api", "facts")
    assert saved.version == VERSION_CREATED
    assert saved.usage_count == USAGE_NEVER
    assert saved.success_count == USAGE_NEVER
    assert saved.created_at.tzinfo == UTC
    assert saved.updated_at.tzinfo == UTC


async def test_save_upsert_replaces_and_bumps_version(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_ALPHA, CONTENT_B, V_UP)
    created = await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A, ("old",))

    replaced = await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_B, ("new",))

    assert replaced.id == created.id
    assert replaced.version == VERSION_REPLACED
    assert replaced.content == CONTENT_B
    assert replaced.tags == ("new",)
    stored = await service.get_by_name(TITLE_ALPHA)
    assert stored.version == VERSION_REPLACED
    assert stored.content == CONTENT_B


async def test_save_upsert_recomputes_embedding(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_ALPHA, CONTENT_B, V_UP)
    embedder.vectors[QUERY] = V_RIGHT
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_B)

    hits = await service.search(QUERY, k=1)
    assert len(hits) == 1
    # the query vector no longer matches: the stored embedding moved with the content
    assert hits[0].score == pytest.approx(0.0)


async def test_get_by_name_raises_when_missing(service: InstructionService) -> None:
    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name("missing")


async def test_get_by_name_narrows_by_type(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_ALPHA, CONTENT_B, V_UP)
    knowledge = await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    saved = await service.save(InstructionType.SKILL, TITLE_ALPHA, CONTENT_B)

    assert await service.get_by_name(TITLE_ALPHA, InstructionType.SKILL) == saved
    assert await service.get_by_name(TITLE_ALPHA, InstructionType.KNOWLEDGE) == knowledge
    # without a type filter the oldest record wins
    assert await service.get_by_name(TITLE_ALPHA) == knowledge


async def test_get_by_name_narrowed_type_missing_raises(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name(TITLE_ALPHA, InstructionType.ENDPOINT)


async def test_search_empty_store_returns_nothing(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    embedder.vectors[QUERY] = V_RIGHT

    assert await service.search(QUERY, k=THREE_HITS) == []


async def test_search_blank_query_short_circuits(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    assert await service.search("   ", k=1) == []
    assert embedder.calls == []


async def test_search_non_positive_k_short_circuits_before_embedding(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    assert await service.search(QUERY, k=0) == []
    assert embedder.calls == []  # no paid embed call for an empty request


async def test_search_closer_vector_wins(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_UP)
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_DIAGONAL

    hits = await service.search(QUERY, k=TWO_HITS)

    assert [hit.instruction.title for hit in hits] == [TITLE_BETA, TITLE_ALPHA]
    assert hits[0].score == pytest.approx(0.8)
    assert hits[1].score == pytest.approx(0.6)


async def test_search_exact_title_boost_beats_closer_vector(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_UP)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_RIGHT)
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    # the query vector is closest to beta, but the query equals alpha's title
    embedder.vectors[EXACT_QUERY] = V_RIGHT

    hits = await service.search(EXACT_QUERY, k=TWO_HITS)

    assert [hit.instruction.title for hit in hits] == [TITLE_ALPHA, TITLE_BETA]
    assert hits[0].score > hits[1].score


async def test_search_respects_k(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    for index in range(THREE_HITS):
        register_vector(embedder, f"title-{index}", f"content-{index}", V_RIGHT)
        await service.save(InstructionType.SKILL, f"title-{index}", f"content-{index}")
    embedder.vectors[QUERY] = V_RIGHT

    hits = await service.search(QUERY, k=TWO_HITS)

    assert len(hits) == TWO_HITS


async def test_search_bumps_usage_of_returned_hits_only(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_UP)
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_RIGHT

    await service.search(QUERY, k=1)
    await service.search(QUERY, k=1)

    assert (await service.get_by_name(TITLE_ALPHA)).usage_count == USAGE_TWICE
    assert (await service.get_by_name(TITLE_BETA)).usage_count == USAGE_NEVER


class LenientEmbedder:
    """EmbeddingClient stub returning the same vector for every text."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(V_RIGHT for _ in texts)


def make_lenient_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> InstructionService:
    return LocalInstructionService(SqlAlchemyInstructionStore(session_factory), LenientEmbedder())


async def test_delete_removes_the_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    await service.delete(TITLE_ALPHA, InstructionType.KNOWLEDGE)

    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name(TITLE_ALPHA, InstructionType.KNOWLEDGE)


async def test_delete_raises_for_a_missing_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    with pytest.raises(InstructionNotFoundError):
        await service.delete(TITLE_ALPHA, InstructionType.ENDPOINT)


# --- system flag, protection and adoption (model v3) -------------------------


async def test_save_creates_user_record_by_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    saved = await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    assert saved.system is False
    assert await service.list_system() == []


async def test_save_system_marks_the_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    saved = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("core",))

    assert saved.system is True
    assert saved.tags == ("core",)
    listed = await service.list_system()
    assert [record.title for record in listed] == [TITLE_ALPHA]
    assert (await service.get_by_name(TITLE_ALPHA, InstructionType.SKILL)).system is True


async def test_save_refuses_to_overwrite_a_system_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A)

    with pytest.raises(SystemInstructionError, match="managed by the registry"):
        await service.save(InstructionType.SKILL, TITLE_ALPHA, CONTENT_B)
    # a same-titled record of another type is untouched by the protection
    await service.save(InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_B)


async def test_delete_refuses_a_system_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A)

    with pytest.raises(SystemInstructionError, match="managed by the registry"):
        await service.delete(TITLE_ALPHA, InstructionType.SKILL)
    assert (await service.get_by_name(TITLE_ALPHA, InstructionType.SKILL)).system is True


async def test_save_system_skips_reembedding_an_unchanged_record(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    created = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("core",))

    again = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("core",))

    assert again.id == created.id
    assert again.version == VERSION_CREATED  # no bump: nothing changed
    assert embedder.calls == [(f"{TITLE_ALPHA}{EMBEDDED_TEXT_SEPARATOR}{CONTENT_A}",)]


async def test_save_system_reembeds_when_tags_change(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("core",))

    updated = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("new",))

    assert updated.tags == ("new",)
    assert updated.version == VERSION_REPLACED
    assert len(embedder.calls) == TWO_EMBED_CALLS


async def test_delete_system_removes_the_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A)

    await service.delete_system(TITLE_ALPHA, InstructionType.SKILL)

    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name(TITLE_ALPHA, InstructionType.SKILL)


async def test_delete_system_raises_for_a_missing_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    with pytest.raises(InstructionNotFoundError):
        await service.delete_system(TITLE_ALPHA, InstructionType.KNOWLEDGE)


async def test_save_system_adopts_a_legacy_user_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    legacy = await service.save(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("legacy",))

    adopted = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_B, ("core",))

    assert adopted.id == legacy.id
    assert adopted.system is True
    assert adopted.content == CONTENT_B
    assert adopted.tags == ("core",)
    assert adopted.version == VERSION_REPLACED
    assert (await service.get_by_name(TITLE_ALPHA, InstructionType.SKILL)).system is True


async def test_store_reads_endpoint_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    await service.save(InstructionType.ENDPOINT, TITLE_ALPHA, CONTENT_A)

    stored = await service.get_by_name(TITLE_ALPHA, InstructionType.ENDPOINT)
    assert stored.type is InstructionType.ENDPOINT
