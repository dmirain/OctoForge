"""Pure ranking functions: vectorized cosine scoring, exact-title boost, rerank merge.

Standalone volumes allow brute-force cosine over the whole table; the full
openclaw formula (70/30 + MMR + decay) is a later iteration that swaps this
module without touching the service.

Scoring is numpy-vectorized and GIL-aware: the previous pure-Python loop
froze the event loop for ~850 ms at 10k records (measured 2026-07-26), and
recall runs on nearly every user message — that stall was a stop-the-world
for every dialog in the process. The math is one matrix product; the
tuples-to-array conversion is chunked so the worker thread the service runs
`rank` in keeps yielding the GIL. Measured after: max event-loop gap 19 ms
at 10k records (was 847 ms inline).
"""

from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from octoforge_core.instructions.api import EmbeddedInstruction, SearchHit

# Cosine scores lie in [-1, 1], so adding 2.0 puts an exact-title hit strictly
# above any non-exact hit regardless of vector similarity.
EXACT_TITLE_BOOST = 2.0
ZERO_NORM_SIMILARITY = 0.0


def rank(
    candidates: list[EmbeddedInstruction],
    query: str,
    query_embedding: tuple[float, ...],
    k: int,
) -> list[SearchHit]:
    """Score candidates against the query and return the top-k hits, best first.

    Score = cosine similarity; a candidate whose title equals the query
    (case-insensitively) receives EXACT_TITLE_BOOST so it always sorts first.
    Ties break by title for determinism. A candidate whose embedding is empty
    or of a different dimensionality scores 0 rather than erroring: deferred
    embeddings (a failed backend, a data migration) and a swapped embedding
    model must degrade search, not break it — the reembed sweep and the
    exact-title boost keep such records reachable.
    """
    if k <= 0 or not candidates:
        return []
    scores = _cosine_scores(candidates, query_embedding)
    folded_query = query.casefold()
    hits = [
        SearchHit(
            instruction=candidate.instruction,
            score=score
            + (
                EXACT_TITLE_BOOST if candidate.instruction.title.casefold() == folded_query else 0.0
            ),
        )
        for candidate, score in zip(candidates, scores, strict=True)
    ]
    hits.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    return hits[:k]


# tuples→array conversion is one GIL-holding C call: chunking it inserts
# bytecode boundaries where the interpreter can hand the GIL to the event
# loop thread (~5 ms switch interval), so even a huge table converts without
# a perceptible stall. 256 rows ≈ single-digit milliseconds per chunk.
_CONVERT_CHUNK_ROWS = 256


def _cosine_scores(
    candidates: list[EmbeddedInstruction],
    query_embedding: tuple[float, ...],
) -> list[float]:
    """Vectorized cosine of every candidate against the query (0 where unusable)."""
    scores = [ZERO_NORM_SIMILARITY] * len(candidates)
    query_vector = np.asarray(query_embedding, dtype=np.float32)
    dimensions = query_vector.shape[0]
    query_norm = float(np.linalg.norm(query_vector))
    if dimensions == 0 or query_norm == 0:
        return scores
    usable = [
        index
        for index, candidate in enumerate(candidates)
        if len(candidate.embedding) == dimensions
    ]
    if not usable:
        return scores
    matrix = _to_matrix([candidates[index].embedding for index in usable], dimensions)
    norms = np.linalg.norm(matrix, axis=1)
    dots = matrix @ query_vector
    nonzero = norms > 0
    cosines = np.zeros(len(usable), dtype=np.float32)
    cosines[nonzero] = dots[nonzero] / (norms[nonzero] * query_norm)
    for position, index in enumerate(usable):
        scores[index] = float(cosines[position])
    return scores


def _to_matrix(embeddings: list[tuple[float, ...]], dimensions: int) -> npt.NDArray[np.float32]:
    """Convert embeddings chunk by chunk (GIL-friendly, see _CONVERT_CHUNK_ROWS)."""
    matrix = np.empty((len(embeddings), dimensions), dtype=np.float32)
    for start in range(0, len(embeddings), _CONVERT_CHUNK_ROWS):
        chunk = embeddings[start : start + _CONVERT_CHUNK_ROWS]
        matrix[start : start + len(chunk)] = np.asarray(chunk, dtype=np.float32)
    return matrix


def rerank(
    hits: list[SearchHit],
    scores: tuple[float, ...],
    k: int,
    query: str,
) -> list[SearchHit]:
    """Replace hit scores with cross-encoder scores and return the top-k.

    `scores` aligns with `hits` by position (zip is strict: a length mismatch
    is a bug and raises). Ties break by title for determinism. A hit whose
    title equals the query (case-insensitively) stays first regardless of the
    cross-encoder score: the exact-title guarantee of `rank` must survive the
    rerank stage.
    """
    if k <= 0:
        return []
    rescored = [
        SearchHit(instruction=hit.instruction, score=score)
        for hit, score in zip(hits, scores, strict=True)
    ]
    rescored.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    exact = next(
        (hit for hit in rescored if hit.instruction.title.casefold() == query.casefold()),
        None,
    )
    if exact is None:
        return rescored[:k]
    rest = [hit for hit in rescored if hit is not exact]
    return [exact, *rest][:k]


# Reciprocal Rank Fusion: score = sum over rankings of 1/(RRF_SMOOTHING + rank).
# Chosen over a weighted sum of scores because the two rankings are not on a
# common scale and never will be -- cosine similarity is bounded in [-1, 1]
# while BM25 is unbounded and corpus-dependent, so any normalization would need
# retuning as the corpus grows. RRF reads only positions, so it needs no
# weights and no tuning. 60 is the constant from the original paper; it damps
# the difference between the top few positions so one ranking's confident first
# place cannot by itself outvote agreement further down the other.
RRF_SMOOTHING = 60
FIRST_RANK = 1


def _reciprocal_rank_scores(
    rankings: Sequence[Sequence[EmbeddedInstruction]],
) -> tuple[dict[str, float], dict[str, EmbeddedInstruction]]:
    """Accumulate reciprocal-rank scores and keep one record object per id."""
    scores: dict[str, float] = {}
    records: dict[str, EmbeddedInstruction] = {}
    for ranking in rankings:
        for position, candidate in enumerate(ranking, start=FIRST_RANK):
            key = candidate.instruction.id
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_SMOOTHING + position)
            records.setdefault(key, candidate)
    return scores, records


def fuse_rankings(
    rankings: Sequence[Sequence[EmbeddedInstruction]],
) -> list[EmbeddedInstruction]:
    """Merge ranked candidate lists into one, best first, by reciprocal rank.

    A record appearing in several rankings accumulates their contributions, so
    agreement between independent retrievers wins over a strong showing in one.
    Ties break by title, for the same determinism `rank` guarantees.
    """
    scores, records = _reciprocal_rank_scores(rankings)
    return sorted(
        records.values(),
        key=lambda candidate: (-scores[candidate.instruction.id], candidate.instruction.title),
    )


def fuse(
    rankings: Sequence[Sequence[EmbeddedInstruction]],
    query: str,
    k: int,
) -> list[SearchHit]:
    """Fuse ranked candidate lists into scored hits, best first.

    The counterpart of `rank` for the hybrid path: where `rank` scores by
    cosine because it was handed unordered candidates, this one is handed
    orderings that already encode each retriever's judgement and only has to
    combine them.

    The exact-title guarantee survives: a candidate whose title equals the
    query gets EXACT_TITLE_BOOST, which dwarfs any fused score (with two
    rankings the maximum is 2/61) and therefore always sorts first — the same
    promise `rank` and `rerank` make.
    """
    if k <= 0:
        return []
    scores, records = _reciprocal_rank_scores(rankings)
    folded_query = query.casefold()
    hits = [
        SearchHit(
            instruction=candidate.instruction,
            score=scores[key]
            + (
                EXACT_TITLE_BOOST if candidate.instruction.title.casefold() == folded_query else 0.0
            ),
        )
        for key, candidate in records.items()
    ]
    hits.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    return hits[:k]
