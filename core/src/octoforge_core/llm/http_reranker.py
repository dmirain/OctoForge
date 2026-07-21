"""HTTP reranker over a SiliconFlow-compatible POST /rerank endpoint."""

from dataclasses import dataclass
from typing import Any

import httpx

from octoforge_core.errors import LLMResponseError

DEFAULT_RERANK_API_URL = "https://api.siliconflow.cn/v1/rerank"
DEFAULT_RERANK_TIMEOUT_SECONDS = 30.0


class RerankerError(Exception):
    """Raised when the rerank endpoint cannot serve the request."""


@dataclass(frozen=True, slots=True)
class HttpRerankerConfig:
    """Connection settings of an HTTP reranker backend."""

    model: str
    api_key: str
    api_url: str = DEFAULT_RERANK_API_URL
    timeout_seconds: float = DEFAULT_RERANK_TIMEOUT_SECONDS


class HttpRerankerClient:
    """RerankerClient over an HTTP API (SiliconFlow, vLLM and other Jina-like endpoints).

    The API ranks the documents of one query per call, so the pairs are
    grouped by query; one request is issued per distinct query and the
    scores are mapped back by the returned document index.
    """

    def __init__(self, http_client: httpx.AsyncClient, config: HttpRerankerConfig) -> None:
        self._http = http_client
        self._config = config

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        """Score every pair via the endpoint, preserving the input order."""
        if not pairs:
            return ()
        scores = [0.0] * len(pairs)
        for query, indexes in _group_by_query(pairs).items():
            group_scores = await self._score_one_query(
                query, tuple(pairs[index][1] for index in indexes)
            )
            for index, group_score in zip(indexes, group_scores, strict=True):
                scores[index] = group_score
        return tuple(scores)

    async def _score_one_query(self, query: str, documents: tuple[str, ...]) -> list[float]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "query": query,
            "documents": list(documents),
            "top_n": len(documents),
            "return_documents": False,
        }
        try:
            response = await self._http.post(
                self._config.api_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RerankerError(f"rerank failed: {type(exc).__name__}") from exc
        if response.status_code != httpx.codes.OK:
            raise RerankerError(f"rerank API returned HTTP {response.status_code}")
        return _parse_scores(response.json(), len(documents))


def _group_by_query(pairs: tuple[tuple[str, str], ...]) -> dict[str, list[int]]:
    """Group pair indexes by query, preserving the first-seen order."""
    groups: dict[str, list[int]] = {}
    for index, (query, _) in enumerate(pairs):
        groups.setdefault(query, []).append(index)
    return groups


def _parse_scores(payload: dict[str, Any], expected: int) -> list[float]:
    """Map the sorted results back to the input document order by index."""
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise LLMResponseError("Unexpected rerank response payload")
    scores = [0.0] * expected
    seen = 0
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise LLMResponseError("Unexpected rerank response payload")
        index = raw.get("index")
        score = raw.get("relevance_score")
        if not isinstance(index, int) or not 0 <= index < expected:
            raise LLMResponseError("Unexpected rerank response payload")
        if not isinstance(score, int | float):
            raise LLMResponseError("Unexpected rerank response payload")
        scores[index] = float(score)
        seen += 1
    if seen != expected:
        raise LLMResponseError("Unexpected rerank response payload")
    return scores
