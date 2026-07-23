"""Tests for the http_request basic tool."""

from collections.abc import Callable
from http import HTTPStatus

import httpx
import pytest

from octoforge_core.net.errors import SsrfBlockedError
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tools import (
    MAX_RESPONSE_CHARS,
    REQUEST_NAME,
    TRUNCATED_SUFFIX,
    HttpRequestTool,
)
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.errors import ToolArgumentsError

TARGET_URL = "https://api.example.com/data"
RESPONSE_BODY = "hello body"
PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.0.0.1"
CTX = ToolContext(user_id="user-test", channel="web", dialog_id="dlg-test")


class StubResolver:
    """HostResolver returning a scripted set of addresses."""

    def __init__(self, ips: tuple[str, ...]) -> None:
        self._ips = ips

    async def resolve(self, host: str) -> tuple[str, ...]:
        return self._ips


def make_tool(
    handler: Callable[[httpx.Request], httpx.Response],
    ips: tuple[str, ...] = (PUBLIC_IP,),
) -> HttpRequestTool:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    guard = SsrfGuard(resolver=StubResolver(ips))
    return HttpRequestTool(http_client=client, guard=guard)


def test_spec_advertised_to_llm() -> None:
    tool = make_tool(lambda request: httpx.Response(HTTPStatus.OK))

    spec = tool.spec

    assert spec.name == REQUEST_NAME
    assert spec.parameters_schema["required"] == ["method", "url"]


async def test_get_request_returns_status_and_body() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text=RESPONSE_BODY)

    tool = make_tool(handler)
    result = await tool.execute({"method": "GET", "url": TARGET_URL}, CTX)

    assert result == f"HTTP {HTTPStatus.OK}\n{RESPONSE_BODY}"
    assert captured[0].method == "GET"
    assert str(captured[0].url) == TARGET_URL


async def test_post_request_sends_body_and_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.CREATED, text="created")

    tool = make_tool(handler)
    result = await tool.execute(
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

    tool = make_tool(handler)
    result = await tool.execute({"method": "GET", "url": TARGET_URL}, CTX)

    assert result == f"HTTP {HTTPStatus.NOT_FOUND}\nnope"


async def test_ssrf_block_prevents_the_request() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK)

    tool = make_tool(handler, ips=(PRIVATE_IP,))

    with pytest.raises(SsrfBlockedError):
        await tool.execute({"method": "GET", "url": TARGET_URL}, CTX)
    assert captured == []


async def test_long_body_is_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, text="x" * (MAX_RESPONSE_CHARS + 10))

    tool = make_tool(handler)
    result = await tool.execute({"method": "GET", "url": TARGET_URL}, CTX)

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
    tool = make_tool(lambda request: httpx.Response(HTTPStatus.OK))

    with pytest.raises(ToolArgumentsError):
        await tool.execute(arguments, CTX)
