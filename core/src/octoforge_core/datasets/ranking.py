"""Pure ranking functions for dataset search: cosine similarity plus exact-name boost.

Deliberately duplicates `instructions/ranking.py`: the datasets module is
self-contained and must not import from the instructions module.
"""

import math
from collections.abc import Sequence

from octoforge_core.datasets.requests import DatasetRankingRequest
from octoforge_core.datasets.types import DatasetHit, EmbeddedDataset

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


def rank(request: DatasetRankingRequest) -> list[DatasetHit]:
    """Score candidates against the query and return the top-k hits, best first.

    Score = cosine similarity; a candidate whose name equals the query
    (case-insensitively) receives EXACT_NAME_BOOST so it always sorts first.
    Ties break by name for determinism.
    """
    if request.limit <= 0:
        return []
    scored = [
        DatasetHit(
            dataset=candidate.dataset,
            score=_score(candidate, request.query, request.query_embedding),
        )
        for candidate in request.candidates
    ]
    scored.sort(key=lambda hit: (-hit.score, hit.dataset.name))
    return scored[: request.limit]


def _score(
    candidate: EmbeddedDataset,
    query: str,
    query_embedding: tuple[float, ...],
) -> float:
    score = cosine_similarity(query_embedding, candidate.embedding)
    if candidate.dataset.name.casefold() == query.casefold():
        score += EXACT_NAME_BOOST
    return score


# See instructions/ranking.py for why reciprocal rank and not a weighted sum:
# cosine is bounded and BM25 is not, so no normalization between them stays
# valid as the corpus grows. Duplicated rather than imported because this
# module must not depend on the instructions module.
RRF_SMOOTHING = 60
FIRST_RANK = 1


def fuse(
    rankings: Sequence[Sequence[EmbeddedDataset]],
    query: str,
    k: int,
) -> list[DatasetHit]:
    """Merge ranked candidate lists into scored hits, best first.

    The exact-name guarantee survives: a descriptor named exactly as the query
    gets EXACT_NAME_BOOST, which dwarfs any fused score (with two rankings the
    maximum is 2/61), so naming a dataset still finds that dataset.
    """
    if k <= 0:
        return []
    scores: dict[str, float] = {}
    records: dict[str, EmbeddedDataset] = {}
    for ranking in rankings:
        for position, candidate in enumerate(ranking, start=FIRST_RANK):
            key = candidate.dataset.id
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_SMOOTHING + position)
            records.setdefault(key, candidate)
    folded_query = query.casefold()
    hits = [
        DatasetHit(
            dataset=candidate.dataset,
            score=scores[key]
            + (EXACT_NAME_BOOST if candidate.dataset.name.casefold() == folded_query else 0.0),
        )
        for key, candidate in records.items()
    ]
    hits.sort(key=lambda hit: (-hit.score, hit.dataset.name))
    return hits[:k]
