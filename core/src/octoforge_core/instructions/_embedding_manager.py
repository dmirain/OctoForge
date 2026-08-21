"""Embedding writes, deferred failures, and bounded model-change repair."""

import logging

from octoforge_core.instructions.ports import InstructionStore
from octoforge_core.instructions.search_policy import InstructionSearchPolicy
from octoforge_core.llm.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)
EMBEDDED_TEXT_SEPARATOR = "\n"


class InstructionEmbeddingManager:
    """Stamp vectors with their model and repair stale records in bounded batches."""

    def __init__(
        self,
        store: InstructionStore,
        embedder: EmbeddingClient,
        policy: InstructionSearchPolicy,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._model = policy.embedding_model
        self._resync_batch = policy.resync_batch

    @property
    def model(self) -> str:
        return self._model

    async def strict(self, title: str, content: str) -> tuple[float, ...]:
        (embedding,) = await self._embedder.embed((embedded_text(title, content),))
        return embedding

    async def lenient(self, title: str, content: str) -> tuple[float, ...]:
        try:
            return await self.strict(title, content)
        except Exception:
            logger.warning(
                "embedding failed, saving %r without a vector (reembed sweep will fix it)",
                title,
                exc_info=True,
            )
            return ()

    async def resync(self) -> int:
        pending = await self._store.list_stale_embeddings(self._model, self._resync_batch)
        if not pending:
            return 0
        embeddings = await self._embedder.embed(
            tuple(embedded_text(record.title, record.content) for record in pending)
        )
        stored = 0
        for record, embedding in zip(pending, embeddings, strict=True):
            if await self._store.set_embedding(record.id, embedding, self._model):
                stored += 1
        return stored


def embedded_text(title: str, content: str) -> str:
    return f"{title}{EMBEDDED_TEXT_SEPARATOR}{content}"
