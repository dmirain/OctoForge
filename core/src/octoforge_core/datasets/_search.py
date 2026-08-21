"""Dataset candidate retrieval and hybrid ranking orchestration."""

from octoforge_core.datasets.ranking import fuse, rank
from octoforge_core.datasets.requests import DatasetRankingRequest
from octoforge_core.datasets.store_ports import (
    DatasetLexicalSearch,
    DatasetStore,
    DatasetVectorSearch,
)
from octoforge_core.datasets.types import DatasetHit, EmbeddedDataset
from octoforge_core.llm.embeddings import EmbeddingClient


class DatasetSearch:
    """Find vector and lexical candidates behind one search operation."""

    def __init__(self, store: DatasetStore, embedder: EmbeddingClient) -> None:
        self._store = store
        self._embedder = embedder

    async def search(self, owner_user_id: str, query: str, limit: int) -> list[DatasetHit]:
        if not query.strip():
            return []
        (query_embedding,) = await self._embedder.embed((query,))
        candidates = await self._vector_candidates(owner_user_id, query_embedding, limit)
        lexical = await self._lexical_candidates(owner_user_id, query, limit)
        if lexical is None:
            return rank(DatasetRankingRequest(candidates, query, query_embedding, limit))
        return fuse([candidates, lexical], query, limit)

    async def _vector_candidates(
        self,
        owner_user_id: str,
        query_embedding: tuple[float, ...],
        limit: int,
    ) -> list[EmbeddedDataset]:
        if isinstance(self._store, DatasetVectorSearch):
            return await self._store.search_by_vector(owner_user_id, query_embedding, limit)
        return await self._store.list_with_embeddings(owner_user_id)

    async def _lexical_candidates(
        self,
        owner_user_id: str,
        query: str,
        limit: int,
    ) -> list[EmbeddedDataset] | None:
        if not isinstance(self._store, DatasetLexicalSearch):
            return None
        return await self._store.search_by_text(owner_user_id, query, limit)
