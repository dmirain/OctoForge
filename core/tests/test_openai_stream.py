"""Tests for the streaming mode of the OpenAI-compatible client."""

import json
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from http import HTTPStatus

import httpx
import pytest

from octoforge_core import (
    ChatMessage,
    LLMConfig,
    MessageRole,
    ProviderInternalError,
    RateLimitError,
    ToolCall,
    Usage,
)
from octoforge_core.llm.events import (
    StreamEvent,
    StreamFinished,
    TextDelta,
    ToolCallBroken,
    ToolCallReady,
    ToolCallStarted,
)
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.tools.base import ToolSpec

RETRY_AFTER_SECONDS = 2.0
BASE_URL = "https://llm.example.com/v1"
API_KEY = "secret-key"
MODEL = "test-model"
CALL_ID = "call-7"
SECOND_CALL_ID = "call-8"
TOOL_NAME = "http_request"
SECOND_TOOL_NAME = "web_search"
STOP_AFTER_EVENTS = 2
PROMPT_TOKENS = 1234
COMPLETION_TOKENS = 42
CACHED_TOKENS = 1000
USER_MESSAGE = ChatMessage(role=MessageRole.USER, content="hi")


def make_config() -> LLMConfig:
    return LLMConfig(api_key=API_KEY, model=MODEL)


def sse_body(chunks: list[dict[str, object]]) -> str:
    lines = [f"data: {json.dumps(chunk)}\n" for chunk in chunks]
    lines.append("data: [DONE]\n")
    return "\n".join(lines)


def content_chunk(text: str) -> dict[str, object]:
    return {"choices": [{"delta": {"content": text}}]}


def tool_call_chunk(index: int, call_id: str, name: str, arguments: str) -> dict[str, object]:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                }
            }
        ]
    }


def make_client(chunks: list[dict[str, object]]) -> OpenAICompatibleClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.OK,
            text=sse_body(chunks),
            headers={"Content-Type": "text/event-stream"},
        )

    transport = httpx.MockTransport(handler)
    return OpenAICompatibleClient(
        http_client=httpx.AsyncClient(transport=transport, base_url=BASE_URL),
        config=make_config(),
    )


async def collect(stream: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    return [event async for event in stream]


async def test_stream_yields_text_deltas_and_final_message() -> None:
    client = make_client([content_chunk("Hel"), content_chunk("lo")])

    events = await collect(client.stream([USER_MESSAGE]))

    assert events[0] == TextDelta(text="Hel")
    assert events[1] == TextDelta(text="lo")
    final = events[-1]
    assert isinstance(final, StreamFinished)
    assert final.message == ChatMessage(role=MessageRole.ASSISTANT, content="Hello")


async def test_stream_accumulates_tool_calls() -> None:
    client = make_client(
        [
            tool_call_chunk(0, CALL_ID, TOOL_NAME, '{"method": "GE'),
            tool_call_chunk(0, "", "", 'T", "url": "https://x"}'),
        ]
    )

    events = await collect(client.stream([USER_MESSAGE]))

    final = events[-1]
    assert isinstance(final, StreamFinished)
    assert final.message.content == ""
    assert final.message.tool_calls == (
        ToolCall(id=CALL_ID, name=TOOL_NAME, arguments={"method": "GET", "url": "https://x"}),
    )


async def test_stream_sends_stream_flag_and_tools() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text=sse_body([content_chunk("ok")]))

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        http_client=httpx.AsyncClient(transport=transport, base_url=BASE_URL),
        config=make_config(),
    )
    tools = [ToolSpec(name=TOOL_NAME, description="d", parameters_schema={"type": "object"})]

    await collect(client.stream([USER_MESSAGE], tools=tools))

    payload = json.loads(captured[0].content)
    assert payload["stream"] is True
    assert payload["tools"][0]["function"]["name"] == TOOL_NAME


async def test_stream_aclose_stops_iteration() -> None:
    client = make_client([content_chunk(str(i)) for i in range(10)])

    stream = client.stream([USER_MESSAGE])
    received: list[StreamEvent] = []
    async for event in stream:
        received.append(event)
        if len(received) == STOP_AFTER_EVENTS:
            break
    await stream.aclose()

    assert len(received) == STOP_AFTER_EVENTS


async def test_stream_emits_tool_call_events_on_index_transition() -> None:
    client = make_client(
        [
            tool_call_chunk(0, CALL_ID, TOOL_NAME, '{"method": "GE'),
            tool_call_chunk(0, "", "", 'T"}'),
            tool_call_chunk(1, SECOND_CALL_ID, SECOND_TOOL_NAME, '{"q": "x"}'),
        ]
    )

    events = await collect(client.stream([USER_MESSAGE]))

    assert events[0] == ToolCallStarted(index=0, call_id=CALL_ID, name=TOOL_NAME)
    assert events[1] == ToolCallReady(
        call=ToolCall(id=CALL_ID, name=TOOL_NAME, arguments={"method": "GET"})
    )
    assert events[2] == ToolCallStarted(index=1, call_id=SECOND_CALL_ID, name=SECOND_TOOL_NAME)
    # the last open slot closes right before StreamFinished
    assert events[3] == ToolCallReady(
        call=ToolCall(id=SECOND_CALL_ID, name=SECOND_TOOL_NAME, arguments={"q": "x"})
    )
    final = events[-1]
    assert isinstance(final, StreamFinished)
    assert final.message.tool_calls == (
        ToolCall(id=CALL_ID, name=TOOL_NAME, arguments={"method": "GET"}),
        ToolCall(id=SECOND_CALL_ID, name=SECOND_TOOL_NAME, arguments={"q": "x"}),
    )


async def test_stream_emits_events_for_single_delta_batch() -> None:
    chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": CALL_ID,
                            "function": {"name": TOOL_NAME, "arguments": '{"a": 1}'},
                        },
                        {
                            "index": 1,
                            "id": SECOND_CALL_ID,
                            "function": {"name": SECOND_TOOL_NAME, "arguments": '{"b": 2}'},
                        },
                    ]
                }
            }
        ]
    }
    client = make_client([chunk])

    events = await collect(client.stream([USER_MESSAGE]))

    ready = [event for event in events if isinstance(event, ToolCallReady)]
    assert [event.call.id for event in ready] == [CALL_ID, SECOND_CALL_ID]
    assert isinstance(events[-1], StreamFinished)


async def test_stream_broken_arguments_emit_broken_and_keep_message() -> None:
    client = make_client([tool_call_chunk(0, CALL_ID, TOOL_NAME, '{"method": ')])

    events = await collect(client.stream([USER_MESSAGE]))

    broken = next(event for event in events if isinstance(event, ToolCallBroken))
    assert broken.call_id == CALL_ID
    assert broken.name == TOOL_NAME
    assert broken.raw == '{"method": '
    final = events[-1]
    assert isinstance(final, StreamFinished)
    assert final.message.tool_calls == (ToolCall(id=CALL_ID, name=TOOL_NAME, arguments={}),)


def usage_chunk(prompt: int, completion: int, cached: int | None = None) -> dict[str, object]:
    usage: dict[str, object] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    return {"choices": [], "usage": usage}


async def test_stream_captures_usage_from_usage_only_chunk() -> None:
    client = make_client(
        [content_chunk("hi"), usage_chunk(PROMPT_TOKENS, COMPLETION_TOKENS, CACHED_TOKENS)]
    )

    events = await collect(client.stream([USER_MESSAGE]))

    final = events[-1]
    assert isinstance(final, StreamFinished)
    assert final.usage == Usage(
        prompt_tokens=PROMPT_TOKENS,
        completion_tokens=COMPLETION_TOKENS,
        cached_tokens=CACHED_TOKENS,
    )


async def test_stream_without_usage_chunk_finishes_with_none_usage() -> None:
    client = make_client([content_chunk("hi")])

    events = await collect(client.stream([USER_MESSAGE]))

    final = events[-1]
    assert isinstance(final, StreamFinished)
    assert final.usage is None


async def test_stream_mid_stream_error_chunk_raises_typed_error() -> None:
    client = make_client(
        [content_chunk("Hel"), {"error": {"message": "upstream connection broken"}}]
    )

    with pytest.raises(ProviderInternalError):
        await collect(client.stream([USER_MESSAGE]))


async def test_stream_mid_stream_error_chunk_uses_numeric_code() -> None:
    client = make_client(
        [{"error": {"code": HTTPStatus.TOO_MANY_REQUESTS, "message": "slow down"}}]
    )

    with pytest.raises(RateLimitError):
        await collect(client.stream([USER_MESSAGE]))


async def test_stream_requests_usage_via_stream_options() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, text=sse_body([content_chunk("ok")]))

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleClient(
        http_client=httpx.AsyncClient(transport=transport, base_url=BASE_URL),
        config=make_config(),
    )

    await collect(client.stream([USER_MESSAGE]))

    payload = json.loads(captured[0].content)
    assert payload["stream_options"] == {"include_usage": True}


@contextmanager
def error_server(status: int, body: bytes, headers: str = "") -> Iterator[int]:
    """Serve one real HTTP error response and return its port.

    A real socket, not `MockTransport`: a mocked response arrives with its body
    already read, which is exactly what used to hide the bug below.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def serve() -> None:
        connection, _ = sock.accept()
        connection.recv(65536)
        connection.sendall(
            f"HTTP/1.1 {status} Error\r\nContent-Type: application/json\r\n"
            f"{headers}Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(sock.getsockname()[1])
    finally:
        sock.close()


async def test_http_error_at_stream_start_is_typed_and_retryable() -> None:
    """A 429 before the first event must reach the retry layer as an LLMError.

    Reading the error body of a streaming response requires an explicit
    `aread()`; without it httpx raises `ResponseNotRead` — a RuntimeError that
    the retry decorator (which only knows `LLMError`) let through, so a rate
    limit at stream start failed the run instead of being retried.
    """
    body = b'{"error": {"message": "slow down"}}'
    with error_server(
        HTTPStatus.TOO_MANY_REQUESTS, body, headers=f"Retry-After: {int(RETRY_AFTER_SECONDS)}\r\n"
    ) as port:
        client = OpenAICompatibleClient(
            http_client=httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}/v1"),
            config=make_config(),
        )

        with pytest.raises(RateLimitError) as failure:
            await collect(client.stream([USER_MESSAGE]))

    assert "slow down" in str(failure.value)
    assert failure.value.retry_after == RETRY_AFTER_SECONDS


async def test_server_error_at_stream_start_keeps_the_provider_message() -> None:
    body = b'{"error": {"message": "upstream exploded"}}'
    with error_server(HTTPStatus.INTERNAL_SERVER_ERROR, body) as port:
        client = OpenAICompatibleClient(
            http_client=httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}/v1"),
            config=make_config(),
        )

        with pytest.raises(ProviderInternalError) as failure:
            await collect(client.stream([USER_MESSAGE]))

    assert "upstream exploded" in str(failure.value)
