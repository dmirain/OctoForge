"""Pure ranking functions: cosine similarity, exact-title boost, rerank merge.

Standalone volumes allow brute-force cosine over the whole table; the full
openclaw formula (70/30 + MMR + decay) is a later iteration that swaps this
module without touching the service.
"""

import math

from octoforge_core.instructions.api import EmbeddedInstruction, SearchHit

# Cosine scores lie in [-1, 1], so adding 2.0 puts an exact-title hit strictly
# above any non-exact hit regardless of vector similarity.
EXACT_TITLE_BOOST = 2.0
ZERO_NORM_SIMILARITY = 0.0


def cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Return the cosine similarity of two equal-length vectors (0 for zero norms)."""
    left_norm = math.sqrt(sum(component * component for component in left))
    right_norm = math.sqrt(sum(component * component for component in right))
    if left_norm == 0 or right_norm == 0:
        return ZERO_NORM_SIMILARITY
    dot = sum(
        left_component * right_component
        for left_component, right_component in zip(left, right, strict=True)
    )
    return dot / (left_norm * right_norm)


def rank(
    candidates: list[EmbeddedInstruction],
    query: str,
    query_embedding: tuple[float, ...],
    k: int,
) -> list[SearchHit]:
    """Score candidates against the query and return the top-k hits, best first.

    Score = cosine similarity; a candidate whose title equals the query
    (case-insensitively) receives EXACT_TITLE_BOOST so it always sorts first.
    Ties break by title for determinism.
    """
    if k <= 0:
        return []
    scored = [
        SearchHit(
            instruction=candidate.instruction,
            score=_score(candidate, query, query_embedding),
        )
        for candidate in candidates
    ]
    scored.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    return scored[:k]


def rerank(hits: list[SearchHit], scores: tuple[float, ...], k: int) -> list[SearchHit]:
    """Replace hit scores with cross-encoder scores and return the top-k.

    `scores` aligns with `hits` by position (zip is strict: a length mismatch
    is a bug and raises). Ties break by title for determinism.
    """
    if k <= 0:
        return []
    rescored = [
        SearchHit(instruction=hit.instruction, score=score)
        for hit, score in zip(hits, scores, strict=True)
    ]
    rescored.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    return rescored[:k]


def _score(
    candidate: EmbeddedInstruction,
    query: str,
    query_embedding: tuple[float, ...],
) -> float:
    score = cosine_similarity(query_embedding, candidate.embedding)
    if candidate.instruction.title.casefold() == query.casefold():
        score += EXACT_TITLE_BOOST
    return score
