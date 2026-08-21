"""Vectorized cosine ranking with an exact-title guarantee."""

import numpy as np
import numpy.typing as npt

from octoforge_core.instructions.requests import InstructionRankingRequest
from octoforge_core.instructions.types import EmbeddedInstruction, SearchHit

EXACT_TITLE_BOOST = 2.0
ZERO_NORM_SIMILARITY = 0.0
_CONVERT_CHUNK_ROWS = 256


def rank(request: InstructionRankingRequest) -> list[SearchHit]:
    """Score candidates and return the best hits with deterministic ties."""
    if request.limit <= 0 or not request.candidates:
        return []
    scores = _cosine_scores(request.candidates, request.embedding)
    folded_query = request.query.casefold()
    hits = [
        SearchHit(
            instruction=candidate.instruction,
            score=score
            + (
                EXACT_TITLE_BOOST if candidate.instruction.title.casefold() == folded_query else 0.0
            ),
        )
        for candidate, score in zip(request.candidates, scores, strict=True)
    ]
    hits.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    return hits[: request.limit]


def _cosine_scores(
    candidates: list[EmbeddedInstruction],
    query_embedding: tuple[float, ...],
) -> list[float]:
    """Vectorized cosine of every candidate, with zero for unusable vectors."""
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
    """Convert in chunks so the worker thread periodically yields the GIL."""
    matrix = np.empty((len(embeddings), dimensions), dtype=np.float32)
    for start in range(0, len(embeddings), _CONVERT_CHUNK_ROWS):
        chunk = embeddings[start : start + _CONVERT_CHUNK_ROWS]
        matrix[start : start + len(chunk)] = np.asarray(chunk, dtype=np.float32)
    return matrix
