"""Cross-encoder reranking: the RerankerClient port and a local implementation.

Mirrors the optional second stage of the b2e project: the bi-encoder
shortlists candidates by cosine similarity, then a cross-encoder re-scores
(query, candidate) pairs for the final order. The model loads lazily and
inference runs in a worker thread because the library is synchronous.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol

import torch
from sentence_transformers import CrossEncoder

DEFAULT_MAX_LENGTH = 512
DEFAULT_RERANK_BATCH_SIZE = 32
DEVICE_MPS = "mps"
DEVICE_CPU = "cpu"


class RerankerClient(Protocol):
    """Scores (query, candidate) pairs; a higher score means more relevant."""

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        """Return one relevance score per pair, preserving input order."""
        ...


@dataclass(frozen=True, slots=True)
class RerankerConfig:
    """Settings of the local cross-encoder reranker."""

    model: str
    max_length: int = DEFAULT_MAX_LENGTH
    batch_size: int = DEFAULT_RERANK_BATCH_SIZE


class CrossEncoderReranker:
    """RerankerClient over a local sentence-transformers CrossEncoder."""

    def __init__(self, config: RerankerConfig) -> None:
        self._config = config
        self._model: CrossEncoder | None = None

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

    def _load_model(self) -> CrossEncoder:
        if self._model is None:
            self._model = CrossEncoder(
                self._config.model,
                max_length=self._config.max_length,
                device=_default_device(),
            )
        return self._model


def _default_device() -> str:
    """Prefer Apple Silicon MPS (as in b2e); fall back to CPU."""
    if torch.backends.mps.is_available():
        return DEVICE_MPS
    return DEVICE_CPU
