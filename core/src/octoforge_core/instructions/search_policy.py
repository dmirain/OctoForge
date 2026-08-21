"""Search tuning and result-diversity policy for the local instruction service."""

from dataclasses import dataclass

from octoforge_core.instructions.types import InstructionType, SearchHit
from octoforge_core.llm.reranker import RerankerClient

DEFAULT_RERANK_CANDIDATES = 20
UNKNOWN_EMBEDDING_MODEL = "unknown"
DEFAULT_RESYNC_BATCH = 256


@dataclass(frozen=True, slots=True)
class InstructionSearchPolicy:
    rerank_candidates: int = DEFAULT_RERANK_CANDIDATES
    embedding_model: str = UNKNOWN_EMBEDDING_MODEL
    resync_batch: int = DEFAULT_RESYNC_BATCH


DEFAULT_SEARCH_POLICY = InstructionSearchPolicy()


@dataclass(frozen=True, slots=True)
class InstructionSearchOptions:
    """Optional second-stage ranker plus the policy governing stored vectors."""

    reranker: RerankerClient | None = None
    policy: InstructionSearchPolicy = DEFAULT_SEARCH_POLICY


DEFAULT_SEARCH_OPTIONS = InstructionSearchOptions()


def wanted_kinds(
    kind: InstructionType | None,
    excluded: tuple[InstructionType, ...],
) -> tuple[InstructionType, ...]:
    if kind is not None:
        return (kind,)
    return tuple(candidate for candidate in InstructionType if candidate not in excluded)


def cap_types(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    """Diversify mixed results without starving a dominant type."""
    cap = max(1, -(-limit // 2))
    taken: list[SearchHit] = []
    skipped: list[SearchHit] = []
    per_type: dict[InstructionType, int] = {}
    for hit in hits:
        if len(taken) == limit:
            return taken
        kind = hit.instruction.type
        if per_type.get(kind, 0) == cap:
            skipped.append(hit)
            continue
        per_type[kind] = per_type.get(kind, 0) + 1
        taken.append(hit)
    filled = taken + skipped[: limit - len(taken)]
    filled.sort(key=lambda hit: (-hit.score, hit.instruction.title))
    return filled
