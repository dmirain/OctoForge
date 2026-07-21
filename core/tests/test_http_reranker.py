"""Tests for the HttpRerankerClient (mocked HTTP transport)."""

import json

import httpx
import pytest

from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.http_reranker import (
    HttpRerankerClient,
    HttpRerankerConfig,
    RerankerError,
)

API_KEY = "test-rerank-key"
API_URL = "https://rerank.example/v1/rerank"
MODEL = "BAAI/bge-reranker-v2-m3"
QUERY = "meaning of life"


def make_client(handler: httpx.MockTransport) -> HttpRerankerClient:
    return HttpRerankerClient(
        http_client=httpx.AsyncClient(transport=handler),
        config=HttpRerankerConfig(model=MODEL, api_key=API_KEY, api_url=API_URL),
    )


def rerank_payload(scores: list[float]) -> dict[str, object]:
    """Build a response sorted by score descending, as the real API returns."""
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return {
        "id": "rerank-test",
        "results": [{"index": index, "relevance_score": scores[index]} for index in order],
    }


async def test_score_sends_the_request_and_maps_scores_back_to_input_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == API_URL
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        assert json.loads(request.content) == {
            "model": MODEL,
            "query": QUERY,
            "documents": ["alpha", "beta", "gamma"],
            "top_n": 3,
            "return_documents": False,
        }
        return httpx.Response(200, content=json.dumps(rerank_payload([0.1, 0.9, 0.5])).encode())

    scores = await make_client(httpx.MockTransport(handler)).score(
        ((QUERY, "alpha"), (QUERY, "beta"), (QUERY, "gamma"))
    )

    assert scores == (0.1, 0.9, 0.5)


async def test_score_groups_pairs_by_query_into_one_request_per_query() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        documents = payload["documents"]
        assert isinstance(documents, list)
        payload = rerank_payload([1.0 / (i + 1) for i in range(len(documents))])
        return httpx.Response(200, content=json.dumps(payload).encode())

    scores = await make_client(httpx.MockTransport(handler)).score(
        (("q1", "a"), ("q2", "b"), ("q1", "c"))
    )

    assert [payload["query"] for payload in requests] == ["q1", "q2"]
    assert requests[0]["documents"] == ["a", "c"]
    assert requests[1]["documents"] == ["b"]
    assert scores == (1.0, 1.0, 0.5)


async def test_score_empty_pairs_short_circuits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert await make_client(httpx.MockTransport(handler)).score(()) == ()


async def test_http_error_status_raises_reranker_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"Forbidden")

    with pytest.raises(RerankerError, match="HTTP 403"):
        await make_client(httpx.MockTransport(handler)).score(((QUERY, "doc"),))


async def test_transport_error_raises_reranker_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(RerankerError, match="ConnectError"):
        await make_client(httpx.MockTransport(handler)).score(((QUERY, "doc"),))


async def test_malformed_payload_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"unexpected": True}).encode())

    with pytest.raises(LLMResponseError):
        await make_client(httpx.MockTransport(handler)).score(((QUERY, "doc"),))


async def test_missing_result_index_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"results": [{"index": 0, "relevance_score": 0.7}]}
        return httpx.Response(200, content=json.dumps(payload).encode())

    with pytest.raises(LLMResponseError):
        await make_client(httpx.MockTransport(handler)).score(((QUERY, "a"), (QUERY, "b")))
