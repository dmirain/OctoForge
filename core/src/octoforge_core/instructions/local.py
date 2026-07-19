"""Local in-process implementation of the InstructionService facade.

SQL storage + brute-force cosine ranking; chosen in the composition root and
replaceable (e.g. by an HTTP client of a dedicated instructions service)
without changes to call sites. When a reranker is configured, the cosine
shortlist is re-scored by a cross-encoder (the b2e two-stage pattern).
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.instructions.api import (
    Instruction,
    InstructionNotFoundError,
    InstructionType,
    SearchHit,
)
from octoforge_core.instructions.ranking import rank, rerank
from octoforge_core.instructions.store import InstructionStore
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.reranker import RerankerClient

EMBEDDED_TEXT_SEPARATOR = "\n"
DEFAULT_RERANK_CANDIDATES = 20


class LocalInstructionService:
    """InstructionService over the module-owned SQL table and an embedder."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingClient,
        reranker: RerankerClient | None = None,
        rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    ) -> None:
        self._store = InstructionStore(session_factory)
        self._embedder = embedder
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates

    async def search(self, query: str, k: int) -> list[SearchHit]:
        """Embed the query, rank all records by cosine and bump usage of the hits.

        With a reranker configured, the cosine stage returns a shortlist of
        `rerank_candidates` which the cross-encoder re-scores down to top-k.
        """
        if not query.strip():
            return []
        (query_embedding,) = await self._embedder.embed((query,))
        candidates = await self._store.list_with_embeddings()
        shortlist = rank(candidates, query, query_embedding, self._shortlist_size(k))
        hits = await self._apply_reranker(query, shortlist, k)
        await self._store.bump_usage(tuple(hit.instruction.id for hit in hits))
        return hits

    async def save(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Embed title + content and upsert the record (version bump on replace)."""
        (embedding,) = await self._embedder.embed((_embedded_text(title, content),))
        return await self._store.upsert(kind, title, content, tags, embedding)

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
        """Return the record by title, optionally narrowed by type."""
        instruction = await self._store.get_by_title(name, kind)
        if instruction is None:
            raise InstructionNotFoundError(name)
        return instruction

    async def delete(self, name: str, kind: InstructionType) -> None:
        """Delete the record; raise InstructionNotFoundError when absent."""
        if not await self._store.delete_by_title(name, kind):
            raise InstructionNotFoundError(name)

    def _shortlist_size(self, k: int) -> int:
        return max(k, self._rerank_candidates) if self._reranker is not None else k

    async def _apply_reranker(
        self,
        query: str,
        shortlist: list[SearchHit],
        k: int,
    ) -> list[SearchHit]:
        if self._reranker is None or not shortlist:
            return shortlist
        pairs = tuple(
            (query, _embedded_text(hit.instruction.title, hit.instruction.content))
            for hit in shortlist
        )
        scores = await self._reranker.score(pairs)
        return rerank(shortlist, scores, k)


def _embedded_text(title: str, content: str) -> str:
    return f"{title}{EMBEDDED_TEXT_SEPARATOR}{content}"
