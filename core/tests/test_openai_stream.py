"""Tests for the streaming mode of the OpenAI-compatible client."""

import json
from collections.abc import AsyncIterator
from http import HTTPStatus

import httpx

from octoforge_core import ChatMessage, LLMConfig, MessageRole, ToolCall
from octoforge_core.llm.events import (
    StreamEvent,
    StreamFinished,
    TextDelta,
    ToolCallBroken,
    ToolCallReady,
    ToolCallStarted,
)
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.skills.base import SkillSpec

BASE_URL = "https://llm.example.com/v1"
API_KEY = "secret-key"
MODEL = "test-model"
CALL_ID = "call-7"
SECOND_CALL_ID = "call-8"
TOOL_NAME = "http_request"
SECOND_TOOL_NAME = "web_search"
STOP_AFTER_EVENTS = 2
USER_MESSAGE = ChatMessage(role=MessageRole.USER, content="hi")


def make_config() -> LLMConfig:
    return LLMConfig(base_url=BASE_URL, api_key=API_KEY, model=MODEL)


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
    tools = [SkillSpec(name=TOOL_NAME, description="d", parameters_schema={"type": "object"})]

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
