"""Tests for the OpenAI-compatible client."""

import json
from http import HTTPStatus

import httpx
import pytest

from octoforge_core import (
    ChatMessage,
    LLMConfig,
    LLMResponseError,
    MessageRole,
    ProviderInternalError,
    ToolCall,
    Usage,
)
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.skills.base import SkillSpec

BASE_URL = "https://llm.example.com/v1"
API_KEY = "secret-key"
MODEL = "test-model"
REPLY_CONTENT = "hello there"
TOOL_NAME = "http_request"
TOOL_DESCRIPTION = "does http"
CALL_ID = "call-42"
PROMPT_TOKENS = 321
COMPLETION_TOKENS = 12
CACHED_TOKENS = 300


def make_config() -> LLMConfig:
    return LLMConfig(api_key=API_KEY, model=MODEL)


def success_response() -> httpx.Response:
    return httpx.Response(
        HTTPStatus.OK,
        json={"choices": [{"message": {"role": "assistant", "content": REPLY_CONTENT}}]},
    )


async def test_complete_sends_request_and_returns_reply() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return success_response()

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        completion = await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    assert completion.message == ChatMessage(role=MessageRole.ASSISTANT, content=REPLY_CONTENT)
    assert completion.usage is None

    request = captured[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "stream_options" not in body


async def test_complete_parses_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.OK,
            json={
                "choices": [{"message": {"role": "assistant", "content": REPLY_CONTENT}}],
                "usage": {
                    "prompt_tokens": PROMPT_TOKENS,
                    "completion_tokens": COMPLETION_TOKENS,
                    "prompt_tokens_details": {"cached_tokens": CACHED_TOKENS},
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        completion = await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    assert completion.usage == Usage(
        prompt_tokens=PROMPT_TOKENS,
        completion_tokens=COMPLETION_TOKENS,
        cached_tokens=CACHED_TOKENS,
    )


async def test_complete_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        with pytest.raises(ProviderInternalError):
            await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])


async def test_complete_raises_on_malformed_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, json={"unexpected": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        with pytest.raises(LLMResponseError):
            await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])


async def test_complete_raises_on_invalid_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(HTTPStatus.OK, text="not a json body")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        with pytest.raises(LLMResponseError):
            await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])


async def test_tools_serialized_into_payload() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return success_response()

    tools = [
        SkillSpec(
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            parameters_schema={"type": "object"},
        )
    ]
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        await client.complete([ChatMessage(role=MessageRole.USER, content="hi")], tools=tools)

    payload = json.loads(captured[0].content)
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "parameters": {"type": "object"},
            },
        }
    ]


async def test_tool_calls_parsed_from_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.OK,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": CALL_ID,
                                    "type": "function",
                                    "function": {
                                        "name": TOOL_NAME,
                                        "arguments": '{"method": "GET", "url": "https://x"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        completion = await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])

    assert completion.message.content == ""
    assert completion.message.tool_calls == (
        ToolCall(id=CALL_ID, name=TOOL_NAME, arguments={"method": "GET", "url": "https://x"}),
    )


async def test_complete_raises_on_non_object_tool_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            HTTPStatus.OK,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": CALL_ID,
                                    "type": "function",
                                    "function": {"name": TOOL_NAME, "arguments": "[1, 2]"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        with pytest.raises(LLMResponseError, match="not a JSON object"):
            await client.complete([ChatMessage(role=MessageRole.USER, content="hi")])


async def test_tool_history_serialized_into_payload() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return success_response()

    history = [
        ChatMessage(role=MessageRole.USER, content="hi"),
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id=CALL_ID, name=TOOL_NAME, arguments={"a": 1}),),
        ),
        ChatMessage(role=MessageRole.TOOL, content="output", tool_call_id=CALL_ID),
    ]
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as http:
        client = OpenAICompatibleClient(http_client=http, config=make_config())
        await client.complete(history)

    payload = json.loads(captured[0].content)
    assistant = payload["messages"][1]
    tool = payload["messages"][2]
    assert assistant["tool_calls"] == [
        {
            "id": CALL_ID,
            "type": "function",
            "function": {"name": TOOL_NAME, "arguments": '{"a": 1}'},
        }
    ]
    assert tool == {"role": "tool", "content": "output", "tool_call_id": CALL_ID}
