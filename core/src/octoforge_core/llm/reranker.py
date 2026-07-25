"""Cross-encoder reranking: the RerankerClient port and a local implementation.

Mirrors the optional second stage of the b2e project: the bi-encoder
shortlists candidates by cosine similarity, then a cross-encoder re-scores
(query, candidate) pairs for the final order. The model loads lazily and
inference runs in a worker thread because the library is synchronous.

torch/sentence-transformers are the optional `local-embeddings` extra
(`pip install "octoforge-core[local-embeddings]"`) -- importing this module
never requires them, only constructing CrossEncoderReranker does.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Protocol

from octoforge_core.config import RerankerConfig

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder as _CrossEncoderType

try:
    import torch
    from sentence_transformers import CrossEncoder
except ImportError:  # pragma: no cover - exercised by installs without the extra
    torch = None  # type: ignore[assignment]
    CrossEncoder = None  # type: ignore[misc]

__all__ = ["CrossEncoderReranker", "RerankerClient", "RerankerConfig"]

DEVICE_MPS = "mps"
DEVICE_CPU = "cpu"

_INSTALL_HINT = 'install the optional extra: pip install "octoforge-core[local-embeddings]"'


class RerankerClient(Protocol):
    """Scores (query, candidate) pairs; a higher score means more relevant."""

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        """Return one relevance score per pair, preserving input order."""
        ...


class CrossEncoderReranker:
    """RerankerClient over a local sentence-transformers CrossEncoder."""

    def __init__(self, config: RerankerConfig) -> None:
        if CrossEncoder is None:
            raise ImportError(f"CrossEncoderReranker needs sentence-transformers; {_INSTALL_HINT}")
        self._config = config
        self._model: _CrossEncoderType | None = None
        self._load_lock = threading.Lock()

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        """Return one cross-encoder score per pair, preserving input order."""
        if not pairs:
            return ()
        return await asyncio.to_thread(self._predict, pairs)

    def _predict(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        model = self._load_model()
        scores = model.predict(
            list(pairs),
            batch_size=self._config.batch_size,
            show_progress_bar=False,
        )
        return tuple(float(score) for score in scores)

    def _load_model(self) -> _CrossEncoderType:
        # The lock serializes concurrent first calls from worker threads:
        # without it two of them would each load their own copy of the model.
        assert CrossEncoder is not None  # __init__ already guards against this
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    self._model = CrossEncoder(
                        self._config.model,
                        max_length=self._config.max_length,
                        device=_default_device(),
                    )
        return self._model


def _default_device() -> str:
    """Prefer Apple Silicon MPS (as in b2e); fall back to CPU."""
    if torch is not None and torch.backends.mps.is_available():
        return DEVICE_MPS
    return DEVICE_CPU
