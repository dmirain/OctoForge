"""Cross-encoder reranking and reciprocal-rank fusion."""

from collections.abc import Sequence

from octoforge_core.instructions._cosine_ranking import EXACT_TITLE_BOOST
from octoforge_core.instructions.requests import (
    InstructionFusionRequest,
    InstructionRerankingRequest,
)
from octoforge_core.instructions.types import EmbeddedInstruction, SearchHit

RRF_SMOOTHING = 60
FIRST_RANK = 1


def rerank(request: InstructionRerankingRequest) -> list[SearchHit]:
    """Replace scores while preserving deterministic and exact-title ordering."""
    if request.limit <= 0:
        return []
    rescored = [
        SearchHit(instruction=hit.instruction, score=score)
        for hit, score in zip(request.hits, request.scores, strict=True)
    ]
    rescored.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    exact = next(
        (hit for hit in rescored if hit.instruction.title.casefold() == request.query.casefold()),
        None,
    )
    if exact is None:
        return rescored[: request.limit]
    rest = [hit for hit in rescored if hit is not exact]
    return [exact, *rest][: request.limit]


def _reciprocal_rank_scores(
    rankings: Sequence[Sequence[EmbeddedInstruction]],
) -> tuple[dict[str, float], dict[str, EmbeddedInstruction]]:
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
    """Merge candidate orderings, rewarding agreement between retrievers."""
    scores, records = _reciprocal_rank_scores(rankings)
    return sorted(
        records.values(),
        key=lambda candidate: (-scores[candidate.instruction.id], candidate.instruction.title),
    )


def fuse(request: InstructionFusionRequest) -> list[SearchHit]:
    """Fuse ranked candidates into scored hits with the exact-title guarantee."""
    if request.limit <= 0:
        return []
    scores, records = _reciprocal_rank_scores(request.rankings)
    folded_query = request.query.casefold()
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
    return hits[: request.limit]
