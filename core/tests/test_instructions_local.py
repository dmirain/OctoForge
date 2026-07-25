"""Contract-style tests for InstructionService implementations.

The suite talks to the facade only (search/save/get_by_name/delete/publish)
and builds the service under test through the `service_factory` fixture, so
the same suite can later validate an HTTP implementation of the protocol by
swapping that one fixture.
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

USER_ID = "user-test"
OTHER_USER_ID = "user-other"

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

    saved = await service.save(
        USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A, ("api", "facts")
    )

    assert saved.id
    assert saved.type is InstructionType.KNOWLEDGE
    assert saved.title == TITLE_ALPHA
    assert saved.content == CONTENT_A
    assert saved.tags == ("api", "facts")
    assert saved.version == VERSION_CREATED
    assert saved.usage_count == USAGE_NEVER
    assert saved.success_count == USAGE_NEVER
    assert saved.owner_id == USER_ID
    assert saved.created_at.tzinfo == UTC
    assert saved.updated_at.tzinfo == UTC


async def test_save_upsert_replaces_and_bumps_version(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_ALPHA, CONTENT_B, V_UP)
    created = await service.save(
        USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A, ("old",)
    )

    replaced = await service.save(
        USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_B, ("new",)
    )

    assert replaced.id == created.id
    assert replaced.version == VERSION_REPLACED
    assert replaced.content == CONTENT_B
    assert replaced.tags == ("new",)
    stored = await service.get_by_name(TITLE_ALPHA, user_id=USER_ID)
    assert stored.version == VERSION_REPLACED
    assert stored.content == CONTENT_B


async def test_save_upsert_recomputes_embedding(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_ALPHA, CONTENT_B, V_UP)
    embedder.vectors[QUERY] = V_RIGHT
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_B)

    hits = await service.search(USER_ID, QUERY, k=1)
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
    knowledge = await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    saved = await service.save(USER_ID, InstructionType.SKILL, TITLE_ALPHA, CONTENT_B)

    assert await service.get_by_name(TITLE_ALPHA, InstructionType.SKILL, USER_ID) == saved
    assert await service.get_by_name(TITLE_ALPHA, InstructionType.KNOWLEDGE, USER_ID) == knowledge
    # without a type filter the oldest record wins
    assert await service.get_by_name(TITLE_ALPHA, user_id=USER_ID) == knowledge


async def test_get_by_name_narrowed_type_missing_raises(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name(TITLE_ALPHA, InstructionType.ENDPOINT, USER_ID)


async def test_search_empty_store_returns_nothing(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    embedder.vectors[QUERY] = V_RIGHT

    assert await service.search(USER_ID, QUERY, k=THREE_HITS) == []


async def test_search_blank_query_short_circuits(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    assert await service.search(USER_ID, "   ", k=1) == []
    assert embedder.calls == []


async def test_search_non_positive_k_short_circuits_before_embedding(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    assert await service.search(USER_ID, QUERY, k=0) == []
    assert embedder.calls == []  # no paid embed call for an empty request


async def test_search_closer_vector_wins(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_UP)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_DIAGONAL

    hits = await service.search(USER_ID, QUERY, k=TWO_HITS)

    assert [hit.instruction.title for hit in hits] == [TITLE_BETA, TITLE_ALPHA]
    assert hits[0].score == pytest.approx(0.8)
    assert hits[1].score == pytest.approx(0.6)


async def test_search_exact_title_boost_beats_closer_vector(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_UP)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_RIGHT)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    # the query vector is closest to beta, but the query equals alpha's title
    embedder.vectors[EXACT_QUERY] = V_RIGHT

    hits = await service.search(USER_ID, EXACT_QUERY, k=TWO_HITS)

    assert [hit.instruction.title for hit in hits] == [TITLE_ALPHA, TITLE_BETA]
    assert hits[0].score > hits[1].score


async def test_search_respects_k(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    for index in range(THREE_HITS):
        register_vector(embedder, f"title-{index}", f"content-{index}", V_RIGHT)
        await service.save(USER_ID, InstructionType.SKILL, f"title-{index}", f"content-{index}")
    embedder.vectors[QUERY] = V_RIGHT

    hits = await service.search(USER_ID, QUERY, k=TWO_HITS)

    assert len(hits) == TWO_HITS


async def test_search_bumps_usage_of_returned_hits_only(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_UP)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_RIGHT

    await service.search(USER_ID, QUERY, k=1)
    await service.search(USER_ID, QUERY, k=1)

    assert (await service.get_by_name(TITLE_ALPHA, user_id=USER_ID)).usage_count == USAGE_TWICE
    assert (await service.get_by_name(TITLE_BETA, user_id=USER_ID)).usage_count == USAGE_NEVER


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
    saved = await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    await service.delete(USER_ID, saved.id)

    with pytest.raises(InstructionNotFoundError):
        await service.get_by_name(TITLE_ALPHA, InstructionType.KNOWLEDGE, USER_ID)


async def test_delete_raises_for_a_missing_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    with pytest.raises(InstructionNotFoundError):
        await service.delete(USER_ID, "missing-id")


async def test_delete_ignores_another_users_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    saved = await service.save(OTHER_USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    with pytest.raises(InstructionNotFoundError):
        await service.delete(USER_ID, saved.id)
    # the owner's record survived the foreign delete attempt
    assert await service.get_by_name(TITLE_ALPHA, user_id=OTHER_USER_ID) == saved


# --- ownership and visibility -------------------------------------------------


async def test_search_hides_another_users_private_records(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    await service.save(OTHER_USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    embedder.vectors[QUERY] = V_RIGHT

    assert await service.search(USER_ID, QUERY, k=THREE_HITS) == []
    assert len(await service.search(OTHER_USER_ID, QUERY, k=THREE_HITS)) == 1


async def test_publish_makes_a_private_record_visible_to_everyone(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    saved = await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    embedder.vectors[QUERY] = V_RIGHT
    assert await service.search(OTHER_USER_ID, QUERY, k=THREE_HITS) == []

    published = await service.publish(saved.id)

    assert published.owner_id is None
    assert len(await service.search(OTHER_USER_ID, QUERY, k=THREE_HITS)) == 1
    # a public record resolves without a user id too
    assert (await service.get_by_name(TITLE_ALPHA)).id == saved.id


async def test_publish_raises_for_an_unknown_id(service: InstructionService) -> None:
    with pytest.raises(InstructionNotFoundError):
        await service.publish("missing-id")


async def test_save_creates_a_private_copy_over_a_public_record(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_ALPHA, CONTENT_B, V_UP)
    public = await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.publish(public.id)

    private = await service.save(OTHER_USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_B)

    assert private.id != public.id
    assert private.owner_id == OTHER_USER_ID
    # the owner's copy shadows the public one for them; others still get public
    assert await service.get_by_name(TITLE_ALPHA, user_id=OTHER_USER_ID) == private
    assert (await service.get_by_name(TITLE_ALPHA, user_id=USER_ID)).id == public.id


async def test_search_kind_filter_narrows_candidates(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_UP)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(USER_ID, InstructionType.SKILL, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_DIAGONAL

    hits = await service.search(USER_ID, QUERY, k=TWO_HITS, kind=InstructionType.SKILL)

    assert [hit.instruction.title for hit in hits] == [TITLE_BETA]


async def test_search_all_sees_private_records_of_everyone(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_RIGHT)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    await service.save(OTHER_USER_ID, InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_RIGHT

    hits = await service.search_all(QUERY, k=THREE_HITS)

    assert {hit.instruction.title for hit in hits} == {TITLE_ALPHA, TITLE_BETA}


# --- system flag, protection and adoption (model v3) -------------------------


async def test_save_creates_user_record_by_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    saved = await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)

    assert saved.system is False
    assert saved.owner_id == USER_ID
    assert await service.list_system() == []


async def test_save_system_marks_the_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    saved = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("core",))

    assert saved.system is True
    assert saved.owner_id is None  # system records are public
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
        await service.save(USER_ID, InstructionType.SKILL, TITLE_ALPHA, CONTENT_B)
    # a same-titled record of another type is untouched by the protection
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_B)


async def test_delete_ignores_a_system_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    saved = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_A)

    # system records are public: an owner-scoped delete sees them as missing
    with pytest.raises(InstructionNotFoundError):
        await service.delete(USER_ID, saved.id)
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


async def test_save_system_adopts_a_public_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    legacy = await service.save(USER_ID, InstructionType.SKILL, TITLE_ALPHA, CONTENT_A, ("legacy",))
    await service.publish(legacy.id)  # legacy (pre-ownership) rows migrate to public

    adopted = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_B, ("core",))

    assert adopted.id == legacy.id
    assert adopted.system is True
    assert adopted.content == CONTENT_B
    assert adopted.tags == ("core",)
    assert adopted.version == VERSION_REPLACED
    assert (await service.get_by_name(TITLE_ALPHA, InstructionType.SKILL)).system is True


async def test_save_system_does_not_adopt_a_private_record(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)
    private = await service.save(USER_ID, InstructionType.SKILL, TITLE_ALPHA, CONTENT_A)

    adopted = await service.save_system(InstructionType.SKILL, TITLE_ALPHA, CONTENT_B)

    assert adopted.id != private.id  # a private copy is never adopted by the registry
    assert (await service.get_by_name(TITLE_ALPHA, user_id=USER_ID)).system is False


async def test_store_reads_endpoint_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = make_lenient_service(session_factory)

    await service.save(USER_ID, InstructionType.ENDPOINT, TITLE_ALPHA, CONTENT_A)

    stored = await service.get_by_name(TITLE_ALPHA, InstructionType.ENDPOINT, USER_ID)
    assert stored.type is InstructionType.ENDPOINT


# --- memory records in the shared store --------------------------------------


async def test_publish_refuses_memory_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user's memory is never publishable, whoever asks."""
    service = make_lenient_service(session_factory)
    memory = await service.save(USER_ID, InstructionType.MEMORY, TITLE_ALPHA, CONTENT_A)

    with pytest.raises(InstructionNotFoundError):
        await service.publish(memory.id)
    stored = await service.get_by_name(TITLE_ALPHA, InstructionType.MEMORY, USER_ID)
    assert stored.owner_id == USER_ID


async def test_search_all_hides_memories_unless_asked_explicitly(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    """Admin cross-user search must not casually surface personal memories."""
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_RIGHT)
    await service.save(USER_ID, InstructionType.MEMORY, TITLE_ALPHA, CONTENT_A)
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_RIGHT

    default = await service.search_all(QUERY, k=THREE_HITS)
    explicit = await service.search_all(QUERY, k=THREE_HITS, kind=InstructionType.MEMORY)

    assert [hit.instruction.title for hit in default] == [TITLE_BETA]
    assert [hit.instruction.title for hit in explicit] == [TITLE_ALPHA]


async def test_save_survives_an_embedding_failure(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    """The fact the user asked to remember must not vanish with the backend.

    StubEmbedder raises KeyError for unregistered texts — the save degrades
    to an empty embedding instead of failing, and the record stays findable
    by name until the sweep re-embeds it.
    """
    saved = await service.save(USER_ID, InstructionType.MEMORY, TITLE_ALPHA, CONTENT_A)

    stored = await service.get_by_name(TITLE_ALPHA, InstructionType.MEMORY, USER_ID)
    assert saved.id == stored.id
    assert stored.content == CONTENT_A


async def test_reembed_missing_finishes_deferred_embeddings(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    """The startup sweep turns embedding-less records back into search results."""
    await service.save(USER_ID, InstructionType.MEMORY, TITLE_ALPHA, CONTENT_A)  # embed fails
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)  # backend is back
    embedder.vectors[QUERY] = V_RIGHT

    swept = await service.reembed_missing()
    hits = await service.search(USER_ID, QUERY, k=TWO_HITS, kind=InstructionType.MEMORY)

    assert swept == 1
    assert await service.reembed_missing() == 0  # idempotent: nothing left
    assert [hit.instruction.title for hit in hits] == [TITLE_ALPHA]


# --- endpoint late binding and type caps -------------------------------------


async def test_search_hides_endpoints_unless_asked_explicitly(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    """Skills name endpoints; planning searches must not drown in their records."""
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_RIGHT)
    register_vector(embedder, TITLE_BETA, CONTENT_B, V_RIGHT)
    await service.save(USER_ID, InstructionType.ENDPOINT, TITLE_ALPHA, CONTENT_A)
    await service.save(USER_ID, InstructionType.SKILL, TITLE_BETA, CONTENT_B)
    embedder.vectors[QUERY] = V_RIGHT

    default = await service.search(USER_ID, QUERY, k=THREE_HITS)
    explicit = await service.search(USER_ID, QUERY, k=THREE_HITS, kind=InstructionType.ENDPOINT)

    assert [hit.instruction.title for hit in default] == [TITLE_BETA]
    assert [hit.instruction.title for hit in explicit] == [TITLE_ALPHA]


async def test_mixed_search_caps_each_types_share(
    service: InstructionService,
    embedder: StubEmbedder,
) -> None:
    """One dominant record type must not push the others out of the top-k."""
    for index in range(4):
        title = f"skill {index}"
        register_vector(embedder, title, CONTENT_A, V_RIGHT)
        await service.save(USER_ID, InstructionType.SKILL, title, CONTENT_A)
    register_vector(embedder, TITLE_ALPHA, CONTENT_A, V_DIAGONAL)  # ranks below the skills
    await service.save(USER_ID, InstructionType.KNOWLEDGE, TITLE_ALPHA, CONTENT_A)
    embedder.vectors[QUERY] = V_RIGHT

    hits = await service.search(USER_ID, QUERY, k=THREE_HITS)

    types = [hit.instruction.type for hit in hits]
    # cap = ceil(3/2) = 2 skills; the knowledge record backfills the last slot
    assert types.count(InstructionType.SKILL) == TWO_HITS
    assert InstructionType.KNOWLEDGE in types
