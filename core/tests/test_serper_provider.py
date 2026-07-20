"""Tests for the SerperSearchProvider (mocked HTTP transport)."""

import json

import httpx
import pytest

from octoforge_core.search.api import SearchError
from octoforge_core.search.serper import SerperSearchProvider

API_KEY = "test-serper-key"
QUERY = "meaning of life"
NUM_RESULTS = 5
HTTP_FORBIDDEN = 403


def make_provider(handler: httpx.MockTransport) -> SerperSearchProvider:
    return SerperSearchProvider(
        http_client=httpx.AsyncClient(transport=handler),
        api_key=API_KEY,
    )


def organic_payload() -> dict[str, object]:
    return {
        "answerBox": {"answer": "42"},
        "organic": [
            {"title": "First", "link": "https://first.example", "snippet": "alpha"},
            {"title": "Second", "link": "https://second.example"},
            {"no-title": True},
        ],
    }


async def test_search_sends_the_key_and_parses_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == API_KEY
        assert json.loads(request.content) == {"q": QUERY, "num": NUM_RESULTS}
        return httpx.Response(200, content=json.dumps(organic_payload()).encode())

    response = await make_provider(httpx.MockTransport(handler)).search(QUERY, NUM_RESULTS)

    assert response.answer == "42"
    assert [(r.title, r.link, r.snippet) for r in response.results] == [
        ("First", "https://first.example", "alpha"),
        ("Second", "https://second.example", ""),
    ]


async def test_results_are_capped_at_num_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(organic_payload()).encode())

    response = await make_provider(httpx.MockTransport(handler)).search(QUERY, 1)

    assert len(response.results) == 1


async def test_answer_box_falls_back_to_snippet() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"answerBox": {"snippet": "from snippet"}, "organic": []}
        return httpx.Response(200, content=json.dumps(payload).encode())

    response = await make_provider(httpx.MockTransport(handler)).search(QUERY, NUM_RESULTS)

    assert response.answer == "from snippet"


async def test_non_ok_status_raises_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTP_FORBIDDEN, content=b"Forbidden")

    with pytest.raises(SearchError, match="search API returned HTTP 403"):
        await make_provider(httpx.MockTransport(handler)).search(QUERY, NUM_RESULTS)


async def test_network_failure_raises_search_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(SearchError, match="search failed: ConnectError"):
        await make_provider(httpx.MockTransport(handler)).search(QUERY, NUM_RESULTS)
