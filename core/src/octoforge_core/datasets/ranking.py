"""Pure ranking functions for dataset search: cosine similarity plus exact-name boost.

Deliberately duplicates `instructions/ranking.py`: the datasets module is
self-contained and must not import from the instructions module.
"""

import math

from octoforge_core.datasets.api import DatasetHit, EmbeddedDataset

# Cosine scores lie in [-1, 1], so adding 2.0 puts an exact-name hit strictly
# above any non-exact hit regardless of vector similarity.
EXACT_NAME_BOOST = 2.0
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
    candidates: list[EmbeddedDataset],
    query: str,
    query_embedding: tuple[float, ...],
    k: int,
) -> list[DatasetHit]:
    """Score candidates against the query and return the top-k hits, best first.

    Score = cosine similarity; a candidate whose name equals the query
    (case-insensitively) receives EXACT_NAME_BOOST so it always sorts first.
    Ties break by name for determinism.
    """
    if k <= 0:
        return []
    scored = [
        DatasetHit(
            dataset=candidate.dataset,
            score=_score(candidate, query, query_embedding),
        )
        for candidate in candidates
    ]
    scored.sort(key=lambda hit: (-hit.score, hit.dataset.name))
    return scored[:k]


def _score(
    candidate: EmbeddedDataset,
    query: str,
    query_embedding: tuple[float, ...],
) -> float:
    score = cosine_similarity(query_embedding, candidate.embedding)
    if candidate.dataset.name.casefold() == query.casefold():
        score += EXACT_NAME_BOOST
    return score
