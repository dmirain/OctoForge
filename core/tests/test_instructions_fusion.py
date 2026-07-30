"""Reciprocal rank fusion: the arithmetic that merges two retrievers.

Pure functions, so these are the cheap tests. What they pin down is the
behaviour the hybrid search is bought for: a record both retrievers like beats
one that only one of them loves, and the exact-title promise the rest of the
ranking pipeline makes survives fusion.
"""

from datetime import UTC, datetime

from octoforge_core.instructions.api import EmbeddedInstruction, Instruction, InstructionType
from octoforge_core.instructions.ranking import (
    EXACT_TITLE_BOOST,
    RRF_SMOOTHING,
    fuse,
    fuse_rankings,
)

NOW = datetime(2026, 7, 30, tzinfo=UTC)
TOP_K = 10
SMALL_K = 3
OVERSUPPLY = 20
EMBEDDING = (1.0, 0.0)


def candidate(title: str) -> EmbeddedInstruction:
    """A candidate whose id equals its title, so orderings read plainly."""
    return EmbeddedInstruction(
        instruction=Instruction(
            id=title,
            type=InstructionType.SKILL,
            title=title,
            content=f"body of {title}",
            tags=(),
            version=1,
            usage_count=0,
            success_count=0,
            created_at=NOW,
            updated_at=NOW,
        ),
        embedding=EMBEDDING,
    )


def titles(hits: list[EmbeddedInstruction]) -> list[str]:
    return [hit.instruction.title for hit in hits]


def test_agreement_between_retrievers_beats_one_strong_opinion() -> None:
    """The whole reason to fuse: two mediocre votes outrank one first place."""
    vector = [candidate("solo"), candidate("agreed"), candidate("filler")]
    lexical = [candidate("other"), candidate("agreed")]

    fused = fuse_rankings([vector, lexical])

    assert titles(fused)[0] == "agreed"


def test_a_record_only_one_retriever_found_still_survives() -> None:
    """Lexical-only hits are the point: an exact term no embedding placed near."""
    vector = [candidate("semantic")]
    lexical = [candidate("literal")]

    fused = fuse_rankings([vector, lexical])

    assert set(titles(fused)) == {"semantic", "literal"}


def test_ties_break_by_title_so_results_are_reproducible() -> None:
    """Same score must mean same order, or identical queries drift between runs."""
    first = fuse_rankings([[candidate("beta"), candidate("alpha")]])
    second = fuse_rankings([[candidate("beta"), candidate("alpha")]])

    assert titles(first) == titles(second)


def test_an_empty_ranking_contributes_nothing() -> None:
    """A query no word matched must not disturb the other retriever's order."""
    vector = [candidate("first"), candidate("second")]

    assert titles(fuse_rankings([vector, []])) == titles(fuse_rankings([vector]))


def test_exact_title_still_wins_over_any_fused_score() -> None:
    """The promise `rank` and `rerank` make has to hold on the hybrid path too.

    An exact title match is a user naming a record, not describing one; if
    fusion could outvote it, `recall("morning_briefing")` would stop reliably
    returning morning_briefing.
    """
    wanted = candidate("morning_briefing")
    # everything else is ranked above it by both retrievers
    noise = [candidate("a"), candidate("b"), candidate("c")]

    hits = fuse([[*noise, wanted], [*noise, wanted]], "morning_briefing", TOP_K)

    assert hits[0].instruction.title == "morning_briefing"
    assert hits[0].score > EXACT_TITLE_BOOST


def test_fused_scores_follow_the_reciprocal_rank_formula() -> None:
    """Pin the arithmetic, so a refactor cannot quietly change the ranking."""
    hits = fuse([[candidate("top")], [candidate("top")]], "unrelated", TOP_K)

    assert hits[0].score == 2 / (RRF_SMOOTHING + 1)


def test_fusion_respects_the_requested_size() -> None:
    hits = fuse([[candidate(str(index)) for index in range(OVERSUPPLY)]], "unrelated", SMALL_K)

    assert len(hits) == SMALL_K
