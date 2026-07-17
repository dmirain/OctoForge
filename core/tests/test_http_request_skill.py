"""Tests for the http_request basic skill."""

from collections.abc import Callable
from http import HTTPStatus

import httpx
import pytest

from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.basic.http_request import (
    MAX_RESPONSE_CHARS,
    SKILL_NAME,
    TRUNCATED_SUFFIX,
    HttpRequestSkill,
)
from octoforge_core.skills.errors import SkillArgumentsError

TARGET_URL = "https://api.example.com/data"
RESPONSE_BODY = "hello body"
CTX = SkillContext(user_id="user-test", channel="web", dialog_id="dlg-test")


def make_skill(handler: Callable[[httpx.Request], httpx.Response]) -> HttpRequestSkill:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpRequestSkill(http_client=client)


def test_spec_advertised_to_llm() -> None:
    skill = make_skill(lambda request: httpx.Response(HTTPStatus.OK))

    spec = skill.spec

    assert spec.name == SKILL_NAME
    assert spec.parameters_schema["required"] == ["method", "url"]


async def test_get_request_returns_status_and_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text=RESPONSE_BODY)

    skill = make_skill(handler)
    result = await skill.execute({"method": "GET", "url": TARGET_URL}, CTX)

    assert result == f"HTTP {HTTPStatus.OK}\n{RESPONSE_BODY}"
    assert captured[0].method == "GET"
    assert str(captured[0].url) == TARGET_URL


async def test_post_request_sends_body_and_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.CREATED, text="created")

    skill = make_skill(handler)
    result = await skill.execute(
        {
            "method": "POST",
            "url": TARGET_URL,
            "headers": {"X-Token": "abc"},
            "body": '{"a": 1}',
        },
        CTX,
    )

    assert result.startswith(f"HTTP {HTTPStatus.CREATED}")
    assert captured[0].content == b'{"a": 1}'
    assert captured[0].headers["X-Token"] == "abc"


async def test_error_status_returned_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.NOT_FOUND, text="nope")

    skill = make_skill(handler)
    result = await skill.execute({"method": "GET", "url": TARGET_URL}, CTX)

    assert result == f"HTTP {HTTPStatus.NOT_FOUND}\nnope"


async def test_long_body_is_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, text="x" * (MAX_RESPONSE_CHARS + 10))

    skill = make_skill(handler)
    result = await skill.execute({"method": "GET", "url": TARGET_URL}, CTX)

    prefix = f"HTTP {HTTPStatus.OK}\n"
    assert result.endswith(TRUNCATED_SUFFIX)
    assert len(result) <= len(prefix) + MAX_RESPONSE_CHARS + len(TRUNCATED_SUFFIX)


@pytest.mark.parametrize(
    "arguments",
    [
        {"method": "FOO", "url": TARGET_URL},
        {"method": "GET"},
        {"method": "GET", "url": ""},
        {"method": "GET", "url": TARGET_URL, "headers": {"X": 1}},
        {"method": "GET", "url": TARGET_URL, "body": 42},
    ],
)
async def test_invalid_arguments_rejected(arguments: dict[str, object]) -> None:
    skill = make_skill(lambda request: httpx.Response(HTTPStatus.OK))

    with pytest.raises(SkillArgumentsError):
        await skill.execute(arguments, CTX)
