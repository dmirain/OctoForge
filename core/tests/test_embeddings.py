"""Tests for the OpenAI-compatible embeddings client against a mocked transport."""

import json
from collections.abc import Callable
from http import HTTPStatus

import httpx
import pytest

from octoforge_core.config import EmbeddingConfig
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.embeddings import OpenAIEmbeddingClient
from octoforge_core.llm.errors import (
    AuthError,
    ProviderInternalError,
    RateLimitError,
    TransportError,
)

BASE_URL = "https://embed.test/v1"
API_KEY = "test-key"
MODEL = "embed-model"
REQUEST_PATH = "/v1/embeddings"
EXPECTED_ATTEMPTS_WITH_RETRY = 2


async def _no_sleep(delay: float) -> None:
    """Skip the retry backoff in tests."""


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAIEmbeddingClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
    return OpenAIEmbeddingClient(
        http_client=http_client,
        config=EmbeddingConfig(api_key=API_KEY, model=MODEL),
        sleeper=_no_sleep,
    )


def embeddings_payload(vectors: list[list[float]]) -> dict[str, object]:
    return {
        "data": [
            {"index": index, "embedding": vector, "object": "embedding"}
            for index, vector in enumerate(vectors)
        ]
    }


async def test_request_shape_and_response_parsing() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            HTTPStatus.OK,
            json=embeddings_payload([[0.1, 0.2], [0.3, 0.4]]),
        )

    client = make_client(handler)
    vectors = await client.embed(("first", "second"))

    assert vectors == ((0.1, 0.2), (0.3, 0.4))
    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == REQUEST_PATH
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {"model": MODEL, "input": ["first", "second"]}


async def test_empty_input_returns_empty_without_a_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert await make_client(handler).embed(()) == ()


async def test_response_items_are_reordered_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "data": [
                {"index": 1, "embedding": [0.3, 0.4], "object": "embedding"},
                {"index": 0, "embedding": [0.1, 0.2], "object": "embedding"},
            ]
        }
        return httpx.Response(HTTPStatus.OK, json=payload)

    client = make_client(handler)
    vectors = await client.embed(("first", "second"))

    assert vectors == ((0.1, 0.2), (0.3, 0.4))


async def test_rate_limit_raises_typed_error_after_one_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(HTTPStatus.TOO_MANY_REQUESTS, json={"error": {}})

    client = make_client(handler)

    with pytest.raises(RateLimitError):
        await client.embed(("text",))
    assert calls == EXPECTED_ATTEMPTS_WITH_RETRY


async def test_provider_internal_error_is_retried_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR, text="boom")

    client = make_client(handler)

    with pytest.raises(ProviderInternalError):
        await client.embed(("text",))
    assert calls == EXPECTED_ATTEMPTS_WITH_RETRY


async def test_transient_error_is_retried_and_can_recover() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(HTTPStatus.TOO_MANY_REQUESTS, json={"error": {}})
        return httpx.Response(HTTPStatus.OK, json=embeddings_payload([[0.1]]))

    client = make_client(handler)

    assert await client.embed(("text",)) == ((0.1,),)
    assert calls == EXPECTED_ATTEMPTS_WITH_RETRY


async def test_transport_error_raises_typed_error_after_one_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom")

    client = make_client(handler)

    with pytest.raises(TransportError):
        await client.embed(("text",))
    assert calls == EXPECTED_ATTEMPTS_WITH_RETRY


async def test_auth_error_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(HTTPStatus.UNAUTHORIZED, json={"error": {"message": "bad key"}})

    client = make_client(handler)

    with pytest.raises(AuthError):
        await client.embed(("text",))
    assert calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": "not-a-list"},
        {"data": [{"index": 0}]},  # missing embedding
        {"data": [{"index": 0, "embedding": [0.1]}, {"index": 5, "embedding": [0.2]}]},
    ],
)
async def test_malformed_payload_raises_response_error(payload: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, json=payload)

    client = make_client(handler)

    with pytest.raises(LLMResponseError):
        await client.embed(("one", "two"))


async def test_result_count_mismatch_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, json=embeddings_payload([[0.1, 0.2]]))

    client = make_client(handler)

    with pytest.raises(LLMResponseError):
        await client.embed(("one", "two"))
