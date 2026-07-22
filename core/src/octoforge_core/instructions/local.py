"""Local in-process implementation of the InstructionService facade.

The storage layer is the `InstructionStore` port injected through the
constructor: the shipped `SqlAlchemyInstructionStore` ranks brute-force in
the process, an installer can substitute a pgvector/vector-DB store
(implementing `InstructionVectorSearch`) without touching this service.
When a reranker is configured, the cosine shortlist is re-scored by a
cross-encoder (the b2e two-stage pattern); a reranker failure degrades
gracefully to the cosine shortlist.
"""

import logging

from octoforge_core.instructions.api import (
    EmbeddedInstruction,
    Instruction,
    InstructionDraft,
    InstructionNotFoundError,
    InstructionStore,
    InstructionType,
    InstructionVectorSearch,
    SearchHit,
    SystemInstructionError,
)
from octoforge_core.instructions.ranking import rank, rerank
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.reranker import RerankerClient

logger = logging.getLogger(__name__)

EMBEDDED_TEXT_SEPARATOR = "\n"
DEFAULT_RERANK_CANDIDATES = 20
SYSTEM_RECORD_MESSAGE = (
    "'{title}' is a system instruction managed by the registry; it cannot be modified"
)


class LocalInstructionService:
    """InstructionService over an injected store and an embedder."""

    def __init__(
        self,
        store: InstructionStore,
        embedder: EmbeddingClient,
        reranker: RerankerClient | None = None,
        rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._reranker = reranker
        self._rerank_candidates = rerank_candidates

    async def search(self, query: str, k: int) -> list[SearchHit]:
        """Embed the query, rank the candidates by cosine and bump usage of the hits.

        Candidates come from the store: vector-capable stores run the search
        on their side, the rest hand over the whole table for brute-force
        cosine. With a reranker configured, the cosine stage returns a
        shortlist of `rerank_candidates` which the cross-encoder re-scores
        down to top-k.
        """
        if not query.strip() or k <= 0:
            return []
        (query_embedding,) = await self._embedder.embed((query,))
        candidates = await self._candidates(query_embedding, k)
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
        """Embed title + content and upsert the record (version bump on replace).

        Agent-facing: refuses to overwrite a system (registry-owned) record.
        """
        await self._ensure_not_system(kind, title)
        return await self._upsert(kind, title, content, tags, system=False)

    async def save_system(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...] = (),
    ) -> Instruction:
        """Upsert a system record; a (kind, title) match is adopted as system.

        An unchanged system record is returned as-is: re-embedding identical
        title+content on every startup registry sync is a wasted (paid with
        an HTTP backend) call.
        """
        existing = await self._store.get_by_title(title, kind)
        if (
            existing is not None
            and existing.system
            and existing.content == content
            and existing.tags == tags
        ):
            return existing
        return await self._upsert(kind, title, content, tags, system=True)

    async def get_by_name(self, name: str, kind: InstructionType | None = None) -> Instruction:
        """Return the record by title, optionally narrowed by type."""
        instruction = await self._store.get_by_title(name, kind)
        if instruction is None:
            raise InstructionNotFoundError(name)
        return instruction

    async def list_system(self) -> list[Instruction]:
        """Return every system (registry-owned) record."""
        return await self._store.list_system()

    async def delete(self, name: str, kind: InstructionType) -> None:
        """Delete the record; raise InstructionNotFoundError when absent.

        Agent-facing: refuses to delete a system (registry-owned) record.
        """
        existing = await self._store.get_by_title(name, kind)
        if existing is None:
            raise InstructionNotFoundError(name)
        if existing.system:
            raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=name))
        await self._store.delete_by_title(name, kind)

    async def delete_system(self, name: str, kind: InstructionType) -> None:
        """Delete a record regardless of the system flag (registry sync only)."""
        if not await self._store.delete_by_title(name, kind):
            raise InstructionNotFoundError(name)

    async def _upsert(
        self,
        kind: InstructionType,
        title: str,
        content: str,
        tags: tuple[str, ...],
        system: bool,
    ) -> Instruction:
        (embedding,) = await self._embedder.embed((_embedded_text(title, content),))
        draft = InstructionDraft(
            kind=kind,
            title=title,
            content=content,
            tags=tags,
            embedding=embedding,
            system=system,
        )
        return await self._store.upsert(draft)

    async def _ensure_not_system(self, kind: InstructionType, title: str) -> None:
        existing = await self._store.get_by_title(title, kind)
        if existing is not None and existing.system:
            raise SystemInstructionError(SYSTEM_RECORD_MESSAGE.format(title=title))

    def _shortlist_size(self, k: int) -> int:
        return max(k, self._rerank_candidates) if self._reranker is not None else k

    async def _candidates(
        self,
        query_embedding: tuple[float, ...],
        k: int,
    ) -> list[EmbeddedInstruction]:
        """Fetch the ranking input: vector search on the store side when supported."""
        if isinstance(self._store, InstructionVectorSearch):
            return await self._store.search_by_vector(query_embedding, self._shortlist_size(k))
        return await self._store.list_with_embeddings()

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
        try:
            scores = await self._reranker.score(pairs)
        except Exception:
            # The reranker is an optional second stage: an outage must not take
            # down search — degrade to the cosine shortlist.
            logger.warning("reranker failed, falling back to the cosine shortlist", exc_info=True)
            return shortlist[:k]
        return rerank(shortlist, scores, k, query)


def _embedded_text(title: str, content: str) -> str:
    return f"{title}{EMBEDDED_TEXT_SEPARATOR}{content}"
