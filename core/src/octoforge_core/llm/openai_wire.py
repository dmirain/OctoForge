"""OpenAI-compatible request serialization and non-stream response parsing."""

import json
from dataclasses import dataclass
from typing import Any

from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.errors import LLMResponseError
from octoforge_core.tools.base import ToolSpec

PARSE_ERROR_MESSAGE = "Unexpected LLM response payload"
ARGUMENTS_NOT_OBJECT_MESSAGE = "tool call arguments are not a JSON object"
FUNCTION_TOOL_TYPE = "function"


@dataclass(frozen=True, slots=True)
class OpenAIRequest:
    model: str
    messages: list[ChatMessage]
    tools: list[ToolSpec] | None
    stream: bool


def build_payload(request: OpenAIRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [serialize_message(message) for message in request.messages],
        "stream": request.stream,
    }
    if request.stream:
        payload["stream_options"] = {"include_usage": True}
    if request.tools:
        payload["tools"] = [serialize_tool(spec) for spec in request.tools]
    return payload


def serialize_message(message: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": FUNCTION_TOOL_TYPE,
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        data["tool_call_id"] = message.tool_call_id
    return data


def serialize_tool(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": FUNCTION_TOOL_TYPE,
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters_schema,
        },
    }


def parse_reply(data: dict[str, Any]) -> ChatMessage:
    try:
        raw_message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
    if not isinstance(raw_message, dict):
        raise LLMResponseError(PARSE_ERROR_MESSAGE)
    content = raw_message.get("content") or ""
    if not isinstance(content, str):
        raise LLMResponseError(PARSE_ERROR_MESSAGE)
    raw_calls = raw_message.get("tool_calls") or []
    tool_calls = tuple(parse_tool_call(raw) for raw in raw_calls)
    return ChatMessage(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls)


def parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    try:
        arguments = json.loads(raw["function"]["arguments"] or "{}")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
    if not isinstance(arguments, dict):
        raise LLMResponseError(ARGUMENTS_NOT_OBJECT_MESSAGE)
    return ToolCall(id=raw["id"], name=raw["function"]["name"], arguments=arguments)
