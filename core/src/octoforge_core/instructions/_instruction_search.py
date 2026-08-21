"""Search orchestration, reranking, diversity, and asynchronous usage counting."""

import asyncio
import logging

from octoforge_core.instructions._embedding_manager import embedded_text
from octoforge_core.instructions._instruction_retrieval import CandidateRequest, CandidateRetriever
from octoforge_core.instructions.ports import InstructionStore
from octoforge_core.instructions.ranking import rerank
from octoforge_core.instructions.requests import (
    InstructionRerankingRequest,
    InstructionSearchRequest,
)
from octoforge_core.instructions.search_policy import (
    InstructionSearchOptions,
    cap_types,
    wanted_kinds,
)
from octoforge_core.instructions.types import InstructionType, SearchHit
from octoforge_core.llm.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)


class InstructionSearchEngine:
    """Produce ranked visible hits without exposing retrieval implementation details."""

    def __init__(
        self,
        store: InstructionStore,
        embedder: EmbeddingClient,
        options: InstructionSearchOptions,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._retriever = CandidateRetriever(store)
        self._reranker = options.reranker
        self._rerank_candidates = options.policy.rerank_candidates
        self._pending_bumps: set[asyncio.Task[None]] = set()

    async def visible(self, user_id: str, request: InstructionSearchRequest) -> list[SearchHit]:
        return await self._search(user_id, request, (InstructionType.ENDPOINT,))

    async def all(self, request: InstructionSearchRequest) -> list[SearchHit]:
        return await self._search(None, request, (InstructionType.MEMORY,))

    async def _search(
        self,
        user_id: str | None,
        request: InstructionSearchRequest,
        excluded: tuple[InstructionType, ...],
    ) -> list[SearchHit]:
        if not request.query.strip() or request.limit <= 0:
            return []
        mixed = request.kind is None
        fetch = self._shortlist_size(request.limit)
        if mixed and self._reranker is None:
            fetch = max(fetch, request.limit * 3)
        (embedding,) = await self._embedder.embed((request.query,))
        shortlist = await self._retriever.shortlist(
            CandidateRequest(
                request.query,
                embedding,
                fetch,
                user_id,
                wanted_kinds(request.kind, excluded),
            )
        )
        rerank_limit = len(shortlist) if mixed else request.limit
        hits = await self._rerank(request.query, shortlist, rerank_limit)
        if mixed:
            hits = cap_types(hits, request.limit)
        self._bump_later(tuple(hit.instruction.id for hit in hits))
        return hits

    def _shortlist_size(self, limit: int) -> int:
        return max(limit, self._rerank_candidates) if self._reranker is not None else limit

    async def _rerank(
        self,
        query: str,
        shortlist: list[SearchHit],
        limit: int,
    ) -> list[SearchHit]:
        if self._reranker is None or not shortlist:
            return shortlist
        pairs = tuple(
            (query, embedded_text(hit.instruction.title, hit.instruction.content))
            for hit in shortlist
        )
        try:
            scores = await self._reranker.score(pairs)
        except Exception:
            logger.warning("reranker failed, falling back to the cosine shortlist", exc_info=True)
            return shortlist[:limit]
        return rerank(InstructionRerankingRequest(shortlist, scores, query, limit))

    def _bump_later(self, instruction_ids: tuple[str, ...]) -> None:
        if not instruction_ids:
            return
        task = asyncio.create_task(self._bump_quietly(instruction_ids))
        self._pending_bumps.add(task)
        task.add_done_callback(self._pending_bumps.discard)

    async def _bump_quietly(self, instruction_ids: tuple[str, ...]) -> None:
        try:
            await self._store.bump_usage(instruction_ids)
        except Exception:
            logger.warning("usage counters not recorded", exc_info=True)
