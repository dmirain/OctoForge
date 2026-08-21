"""Direct tests of the vectorized ranking: scoring, boost, degraded embeddings."""

from datetime import UTC, datetime

from octoforge_core.instructions.api import EmbeddedInstruction, Instruction, InstructionType
from octoforge_core.instructions.ranking import EXACT_TITLE_BOOST, rank
from octoforge_core.instructions.requests import InstructionRankingRequest

NOW = datetime(2026, 1, 1, tzinfo=UTC)
QUERY = "find something"
V_RIGHT = (1.0, 0.0)
V_UP = (0.0, 1.0)
V_DIAGONAL = (0.6, 0.8)
TWO_HITS = 2


def candidate(title: str, embedding: tuple[float, ...]) -> EmbeddedInstruction:
    return EmbeddedInstruction(
        instruction=Instruction(
            id=title,
            type=InstructionType.KNOWLEDGE,
            title=title,
            content="c",
            tags=(),
            version=1,
            usage_count=0,
            success_count=0,
            created_at=NOW,
            updated_at=NOW,
        ),
        embedding=embedding,
    )


def test_rank_orders_by_cosine() -> None:
    hits = rank(
        InstructionRankingRequest(
            [candidate("far", V_UP), candidate("near", V_RIGHT), candidate("mid", V_DIAGONAL)],
            QUERY,
            V_RIGHT,
            3,
        )
    )

    assert [hit.instruction.title for hit in hits] == ["near", "mid", "far"]


def test_rank_exact_title_beats_closer_vector() -> None:
    hits = rank(
        InstructionRankingRequest(
            [candidate("closer", V_RIGHT), candidate(QUERY.upper(), V_UP)],
            QUERY,
            V_RIGHT,
            1,
        )
    )

    assert hits[0].instruction.title == QUERY.upper()
    assert hits[0].score >= EXACT_TITLE_BOOST


def test_rank_respects_k_and_breaks_ties_by_title() -> None:
    hits = rank(
        InstructionRankingRequest(
            [
                candidate("beta", V_RIGHT),
                candidate("alpha", V_RIGHT),
                candidate("gamma", V_RIGHT),
            ],
            QUERY,
            V_RIGHT,
            TWO_HITS,
        )
    )

    assert [hit.instruction.title for hit in hits] == ["alpha", "beta"]


def test_rank_scores_degraded_embeddings_zero_instead_of_erroring() -> None:
    """Deferred (empty) and stale-dimensioned vectors degrade, never break, search."""
    hits = rank(
        InstructionRankingRequest(
            [
                candidate("empty", ()),
                candidate("wrong-dim", (1.0, 0.0, 0.0)),
                candidate("good", V_RIGHT),
            ],
            QUERY,
            V_RIGHT,
            3,
        )
    )

    by_title = {hit.instruction.title: hit.score for hit in hits}
    assert by_title["good"] > 0
    assert by_title["empty"] == 0.0
    assert by_title["wrong-dim"] == 0.0


def test_rank_zero_query_scores_all_zero_but_boost_survives() -> None:
    hits = rank(
        InstructionRankingRequest(
            [candidate("plain", V_RIGHT), candidate(QUERY, V_RIGHT)],
            QUERY,
            (0.0, 0.0),
            TWO_HITS,
        )
    )

    assert hits[0].instruction.title == QUERY
    assert hits[0].score == EXACT_TITLE_BOOST
    assert hits[1].score == 0.0


def test_rank_empty_inputs() -> None:
    assert rank(InstructionRankingRequest([], QUERY, V_RIGHT, 3)) == []
    assert rank(InstructionRankingRequest([candidate("a", V_RIGHT)], QUERY, V_RIGHT, 0)) == []
