"""In-process sentence-transformer embeddings with lazy model loading."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from octoforge_core.config import DEFAULT_EMBEDDING_BATCH_SIZE

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer
    from sentence_transformers import SentenceTransformer as _SentenceTransformerType
else:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:  # pragma: no cover - exercised without the optional extra
        SentenceTransformer = None

_INSTALL_HINT = 'install the optional extra: pip install "octoforge-core[local-embeddings]"'


class SentenceTransformerEmbedder:
    """Embedding client computing normalized vectors in a worker thread."""

    def __init__(self, model_name: str, batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE) -> None:
        if SentenceTransformer is None:
            raise ImportError(
                f"SentenceTransformerEmbedder needs sentence-transformers; {_INSTALL_HINT}"
            )
        self._model_name = model_name
        self._batch_size = batch_size
        self._model: _SentenceTransformerType | None = None
        self._load_lock = threading.Lock()

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        return await asyncio.to_thread(self._encode, texts)

    def _encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors = self._load_model().encode(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
        )
        return tuple(tuple(float(component) for component in row) for row in vectors)

    def _load_model(self) -> _SentenceTransformerType:
        assert SentenceTransformer is not None
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._model = SentenceTransformer(self._model_name)
        return self._model
