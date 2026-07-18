"""Local embeddings: an EmbeddingClient computing vectors in-process.

Mirrors the approach proven in the b2e project: a bi-encoder loaded via
sentence-transformers, vectors L2-normalized so cosine similarity reduces to
a dot product. The model loads lazily on the first call (it is heavy), and
encoding runs in a worker thread because the library is synchronous.
"""

import asyncio

from sentence_transformers import SentenceTransformer

from octoforge_core.config import DEFAULT_EMBEDDING_BATCH_SIZE


class SentenceTransformerEmbedder:
    """EmbeddingClient that computes embeddings locally with a bi-encoder."""

    def __init__(self, model_name: str, batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE) -> None:
        self._model_name = model_name
        self._batch_size = batch_size
        self._model: SentenceTransformer | None = None

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Return one L2-normalized embedding vector per input text, in order."""
        if not texts:
            return ()
        return await asyncio.to_thread(self._encode, texts)

    def _encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        model = self._load_model()
        vectors = model.encode(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
        )
        return tuple(tuple(float(component) for component in row) for row in vectors)

    def _load_model(self) -> SentenceTransformer:
        if self._model is None:
            self._model = SentenceTransformer(self._model_name)
        return self._model
