"""Tests for the rerank stage of instruction search: the pure merge function
and the LocalInstructionService two-stage pipeline (cosine shortlist ->
cross-encoder rerank), the b2e pattern."""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.instructions.api import Instruction, InstructionType, SearchHit
from octoforge_core.instructions.local import LocalInstructionService
from octoforge_core.instructions.ranking import rerank
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.llm.errors import TransportError
from octoforge_core.time import utc_now

MEMORY_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
QUERY = "find something"
TITLE_A = "alpha fact"
TITLE_B = "beta fact"
TITLE_C = "gamma fact"
CONTENT = "content"
RERANK_CANDIDATES = 2
USER_ID = "user-test"
EXPECTED_PAIRS = 2

V_EXACT = (1.0, 0.0)
V_CLOSE = (0.9, 0.1)
V_FAR = (0.0, 1.0)

SCORE_LOW = 0.1
SCORE_HIGH = 0.9
SCORE_MID = 0.5


def make_instruction(title: str) -> Instruction:
    """Build a minimal instruction DTO with the given title."""
    return Instruction(
        id=title,
        type=InstructionType.KNOWLEDGE,
        title=title,
        content=CONTENT,
        tags=(),
        version=1,
        usage_count=0,
        success_count=0,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def make_hit(title: str, score: float) -> SearchHit:
    return SearchHit(instruction=make_instruction(title), score=score)


class StubEmbedder:
    """Deterministic EmbeddingClient: exact text-to-vector mapping."""

    def __init__(self) -> None:
        self.vectors: dict[str, tuple[float, ...]] = {}

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.vectors[text] for text in texts)


class StubReranker:
    """Scripted RerankerClient: returns queued scores, records the pairs."""

    def __init__(self, scores: tuple[float, ...]) -> None:
        self._scores = scores
        self.pairs: tuple[tuple[str, str], ...] = ()

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        self.pairs = pairs
        return self._scores


@pytest.fixture()
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(MEMORY_DATABASE_URL)
    await init_db(engine)
    yield create_session_factory(engine)
    await engine.dispose()


def test_rerank_orders_by_new_scores_and_truncates() -> None:
    hits = [make_hit(TITLE_A, 1.0), make_hit(TITLE_B, 0.9), make_hit(TITLE_C, 0.1)]

    result = rerank(hits, (SCORE_LOW, SCORE_HIGH, SCORE_MID), k=2, query=QUERY)

    assert [hit.instruction.title for hit in result] == [TITLE_B, TITLE_C]
    assert [hit.score for hit in result] == [SCORE_HIGH, SCORE_MID]


def test_rerank_breaks_ties_by_title() -> None:
    hits = [make_hit(TITLE_B, 1.0), make_hit(TITLE_A, 0.9)]

    result = rerank(hits, (SCORE_MID, SCORE_MID), k=2, query=QUERY)

    assert [hit.instruction.title for hit in result] == [TITLE_A, TITLE_B]


def test_rerank_keeps_the_exact_title_first() -> None:
    # TITLE_A matches the query exactly (case-insensitively); the cross-encoder
    # scores it lowest, but the exact-title guarantee of `rank` must survive.
    hits = [make_hit(TITLE_A, 1.0), make_hit(TITLE_B, 0.9), make_hit(TITLE_C, 0.1)]

    result = rerank(hits, (SCORE_LOW, SCORE_HIGH, SCORE_MID), k=2, query=TITLE_A.upper())

    assert [hit.instruction.title for hit in result] == [TITLE_A, TITLE_B]
    single = rerank(hits, (SCORE_LOW, SCORE_HIGH, SCORE_MID), k=1, query=TITLE_A)
    assert [hit.instruction.title for hit in single] == [TITLE_A]


def test_rerank_rejects_mismatched_scores() -> None:
    with pytest.raises(ValueError, match="zip"):
        rerank([make_hit(TITLE_A, 1.0)], (), k=1, query=QUERY)


def test_rerank_returns_nothing_for_non_positive_k() -> None:
    assert rerank([make_hit(TITLE_A, 1.0)], (SCORE_HIGH,), k=0, query=QUERY) == []


async def test_service_reranks_cosine_shortlist(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    embedder = StubEmbedder()
    embedder.vectors[QUERY] = V_EXACT
    reranker = StubReranker(scores=(SCORE_LOW, SCORE_HIGH))
    service = LocalInstructionService(
        SqlAlchemyInstructionStore(session_factory),
        embedder,
        reranker=reranker,
        rerank_candidates=RERANK_CANDIDATES,
    )
    for title, vector in ((TITLE_A, V_EXACT), (TITLE_B, V_CLOSE), (TITLE_C, V_FAR)):
        embedder.vectors[f"{title}\n{CONTENT}"] = vector
        await service.save(USER_ID, InstructionType.KNOWLEDGE, title, CONTENT)

    # Cosine puts TITLE_A first and TITLE_C out of the shortlist; the reranker
    # flips the two shortlisted hits.
    hits = await service.search(USER_ID, QUERY, k=2)

    assert [hit.instruction.title for hit in hits] == [TITLE_B, TITLE_A]
    assert [hit.score for hit in hits] == [SCORE_HIGH, SCORE_LOW]
    assert len(reranker.pairs) == EXPECTED_PAIRS
    assert reranker.pairs[0] == (QUERY, f"{TITLE_A}\n{CONTENT}")
    assert reranker.pairs[1] == (QUERY, f"{TITLE_B}\n{CONTENT}")


async def test_service_without_reranker_keeps_cosine_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    embedder = StubEmbedder()
    embedder.vectors[QUERY] = V_EXACT
    service = LocalInstructionService(SqlAlchemyInstructionStore(session_factory), embedder)
    for title, vector in ((TITLE_A, V_EXACT), (TITLE_B, V_CLOSE)):
        embedder.vectors[f"{title}\n{CONTENT}"] = vector
        await service.save(USER_ID, InstructionType.KNOWLEDGE, title, CONTENT)

    hits = await service.search(USER_ID, QUERY, k=1)

    assert [hit.instruction.title for hit in hits] == [TITLE_A]


class FailingReranker:
    """RerankerClient stub that always raises, like an outage would."""

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        raise TransportError("rerank API unavailable")


async def test_service_falls_back_to_cosine_when_the_reranker_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    embedder = StubEmbedder()
    embedder.vectors[QUERY] = V_EXACT
    service = LocalInstructionService(
        SqlAlchemyInstructionStore(session_factory),
        embedder,
        reranker=FailingReranker(),
        rerank_candidates=RERANK_CANDIDATES,
    )
    for title, vector in ((TITLE_A, V_EXACT), (TITLE_B, V_CLOSE), (TITLE_C, V_FAR)):
        embedder.vectors[f"{title}\n{CONTENT}"] = vector
        await service.save(USER_ID, InstructionType.KNOWLEDGE, title, CONTENT)

    # The failure must not propagate: search degrades to the cosine shortlist.
    hits = await service.search(USER_ID, QUERY, k=2)

    assert [hit.instruction.title for hit in hits] == [TITLE_A, TITLE_B]
    assert hits[0].score > hits[1].score  # cosine scores, not cross-encoder logits
