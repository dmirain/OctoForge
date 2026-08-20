"""Local embeddings: an EmbeddingClient computing vectors in-process.

Mirrors the approach proven in the b2e project: a bi-encoder loaded via
sentence-transformers, vectors L2-normalized so cosine similarity reduces to
a dot product. The model loads lazily on the first call (it is heavy), and
encoding runs in a worker thread because the library is synchronous.

sentence-transformers is the optional `local-embeddings` extra (`pip install
"octoforge-core[local-embeddings]"`) -- importing this module never requires
it, only constructing SentenceTransformerEmbedder does.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from octoforge_core.config import DEFAULT_EMBEDDING_BATCH_SIZE

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer
    from sentence_transformers import SentenceTransformer as _SentenceTransformerType
else:
    # The runtime fallback is hidden from the type checker: analysing the
    # `= None` reassignment produced a different error code with the extra
    # installed (mypy sees the real type) than without it (missing import),
    # so no single `type: ignore` covered both — CI (no extra) and a local
    # install disagreed. Under TYPE_CHECKING mypy always sees the real
    # import; at runtime this branch runs and the guard below stays honest.
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:  # pragma: no cover - exercised by installs without the extra
        SentenceTransformer = None

_INSTALL_HINT = 'install the optional extra: pip install "octoforge-core[local-embeddings]"'


class SentenceTransformerEmbedder:
    """EmbeddingClient that computes embeddings locally with a bi-encoder."""

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

    def _load_model(self) -> _SentenceTransformerType:
        # The lock serializes concurrent first calls from worker threads:
        # without it two of them would each load their own copy of the model.
        assert SentenceTransformer is not None  # __init__ already guards against this
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._model = SentenceTransformer(self._model_name)
        return self._model
