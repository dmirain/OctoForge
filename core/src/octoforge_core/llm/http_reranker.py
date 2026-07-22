"""HTTP reranker over a SiliconFlow-compatible POST /rerank endpoint.

HTTP failures are classified into the shared `llm/errors.py` taxonomy and
transient kinds get one short retry per query group (see `llm/retry.py`).
"""

import asyncio
from typing import Any

import httpx

from octoforge_core.config import HttpRerankerConfig
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.errors import TransportError, raise_for_error_status
from octoforge_core.llm.retry import Sleeper, retry_transient

__all__ = ["HttpRerankerClient", "HttpRerankerConfig"]

PARSE_ERROR_MESSAGE = "Unexpected rerank response payload"


class HttpRerankerClient:
    """RerankerClient over an HTTP API (SiliconFlow, vLLM and other Jina-like endpoints).

    The API ranks the documents of one query per call, so the pairs are
    grouped by query; one request is issued per distinct query and the
    scores are mapped back by the returned document index.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        config: HttpRerankerConfig,
        *,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        self._http = http_client
        self._config = config
        self._sleeper = sleeper

    async def score(self, pairs: tuple[tuple[str, str], ...]) -> tuple[float, ...]:
        """Score every pair via the endpoint, preserving the input order."""
        if not pairs:
            return ()
        scores = [0.0] * len(pairs)
        for query, indexes in _group_by_query(pairs).items():
            group_scores = await self._score_group(
                query, tuple(pairs[index][1] for index in indexes)
            )
            for index, group_score in zip(indexes, group_scores, strict=True):
                scores[index] = group_score
        return tuple(scores)

    async def _score_group(self, query: str, documents: tuple[str, ...]) -> list[float]:
        """Score one query group, retrying transient failures once."""
        return await retry_transient(
            lambda: self._score_one_query(query, documents),
            sleeper=self._sleeper,
        )

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
            raise TransportError(str(exc) or type(exc).__name__) from exc
        raise_for_error_status(response)
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
        raise LLMResponseError(PARSE_ERROR_MESSAGE)
    scores = [0.0] * expected
    seen: set[int] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise LLMResponseError(PARSE_ERROR_MESSAGE)
        index = raw.get("index")
        score = raw.get("relevance_score")
        if not isinstance(index, int) or not 0 <= index < expected:
            raise LLMResponseError(PARSE_ERROR_MESSAGE)
        if not isinstance(score, int | float):
            raise LLMResponseError(PARSE_ERROR_MESSAGE)
        scores[index] = float(score)
        seen.add(index)
    if len(seen) != expected:
        raise LLMResponseError(PARSE_ERROR_MESSAGE)
    return scores
