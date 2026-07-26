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

import math

import numpy as np
import numpy.typing as npt

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
