"""Tests for the serper.dev web_search skill (mocked HTTP transport)."""

import json

import httpx

from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.basic.web_search import WebSearchSkill

API_KEY = "test-serper-key"
CONTEXT = SkillContext(user_id="alice", channel="telegram", dialog_id="dialog-1")
MAX_OUTPUT_WITH_SUFFIX = 4000 + 20


def make_skill(handler: httpx.MockTransport) -> WebSearchSkill:
    return WebSearchSkill(
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


async def test_search_sends_the_key_and_formats_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == API_KEY
        assert json.loads(request.content) == {"q": "meaning of life", "num": 5}
        return httpx.Response(200, content=json.dumps(organic_payload()).encode())

    result = await make_skill(httpx.MockTransport(handler)).execute(
        {"query": "meaning of life"}, CONTEXT
    )

    assert result.startswith("Answer box: 42")
    assert "1. First\nhttps://first.example\nalpha" in result
    assert "2. Second\nhttps://second.example" in result
    assert "no-title" not in result


async def test_num_results_is_clamped_to_the_api_bounds() -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["num"])
        return httpx.Response(200, content=b'{"organic": []}')

    skill = make_skill(httpx.MockTransport(handler))
    await skill.execute({"query": "q", "num_results": 0}, CONTEXT)
    await skill.execute({"query": "q", "num_results": 99}, CONTEXT)
    await skill.execute({"query": "q", "num_results": True}, CONTEXT)

    assert seen == [1, 10, 5]


async def test_empty_answer_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"organic": []}')

    result = await make_skill(httpx.MockTransport(handler)).execute({"query": "q"}, CONTEXT)

    assert result == "no results"


async def test_non_ok_status_is_an_error_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"Forbidden")

    result = await make_skill(httpx.MockTransport(handler)).execute({"query": "q"}, CONTEXT)

    assert result == "error: search API returned HTTP 403"


async def test_network_failure_is_an_error_string() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = await make_skill(httpx.MockTransport(handler)).execute({"query": "q"}, CONTEXT)

    assert result == "error: search failed: ConnectError"


async def test_long_output_is_truncated() -> None:
    payload = {
        "organic": [
            {"title": f"T{i}", "link": f"https://e{i}.example", "snippet": "s" * 900}
            for i in range(10)
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    result = await make_skill(httpx.MockTransport(handler)).execute({"query": "q"}, CONTEXT)

    assert len(result) <= MAX_OUTPUT_WITH_SUFFIX
    assert result.endswith("\n...[truncated]")
