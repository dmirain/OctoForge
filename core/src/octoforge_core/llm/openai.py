"""OpenAI-compatible chat-completion client."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from octoforge_core.config import LLMConfig
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.events import StreamEvent, StreamFinished, TextDelta
from octoforge_core.skills.base import SkillSpec

CHAT_COMPLETIONS_PATH = "/chat/completions"
PARSE_ERROR_MESSAGE = "Unexpected LLM response payload"
FUNCTION_TOOL_TYPE = "function"
SSE_DATA_PREFIX = "data:"
SSE_DONE_MARKER = "[DONE]"


class OpenAICompatibleClient:
    """LLMClient implementation for OpenAI-compatible endpoints."""

    def __init__(self, http_client: httpx.AsyncClient, config: LLMConfig) -> None:
        self._http = http_client
        self._config = config

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> ChatMessage:
        """Call chat/completions (non-streaming) and return the reply."""
        response = await self._http.post(
            CHAT_COMPLETIONS_PATH,
            json=self._build_payload(messages, tools, stream=False),
            headers={"Authorization": f"Bearer {self._config.api_key}"},
            timeout=self._config.timeout_seconds,
        )
        response.raise_for_status()
        return self._parse_reply(response.json())

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Call chat/completions with streaming and yield events."""
        async with self._http.stream(
            "POST",
            CHAT_COMPLETIONS_PATH,
            json=self._build_payload(messages, tools, stream=True),
            headers={"Authorization": f"Bearer {self._config.api_key}"},
            timeout=self._config.timeout_seconds,
        ) as response:
            response.raise_for_status()
            accumulator = _StreamAccumulator()
            async for line in response.aiter_lines():
                if not line.startswith(SSE_DATA_PREFIX):
                    continue
                data = line[len(SSE_DATA_PREFIX) :].strip()
                if data == SSE_DONE_MARKER:
                    break
                for event in accumulator.feed(data):
                    yield event
            yield StreamFinished(message=accumulator.build_message())

    def _build_payload(
        self,
        messages: list[ChatMessage],
        tools: list[SkillSpec] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """Build the chat/completions request payload."""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [self._serialize_message(message) for message in messages],
            "stream": stream,
        }
        if tools:
            payload["tools"] = [self._serialize_tool(spec) for spec in tools]
        return payload

    @staticmethod
    def _serialize_message(message: ChatMessage) -> dict[str, Any]:
        """Convert a domain message into the wire format."""
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

    @staticmethod
    def _serialize_tool(spec: SkillSpec) -> dict[str, Any]:
        """Convert a skill spec into an OpenAI function tool."""
        return {
            "type": FUNCTION_TOOL_TYPE,
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters_schema,
            },
        }

    @classmethod
    def _parse_reply(cls, data: dict[str, Any]) -> ChatMessage:
        """Extract the assistant message from a chat/completions payload."""
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
        tool_calls = tuple(cls._parse_tool_call(raw) for raw in raw_calls)
        return ChatMessage(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls)

    @staticmethod
    def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
        """Extract a single tool call from the wire format."""
        try:
            arguments = json.loads(raw["function"]["arguments"] or "{}")
            return ToolCall(id=raw["id"], name=raw["function"]["name"], arguments=arguments)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc


class _StreamAccumulator:
    """Accumulates streaming deltas into a complete assistant message."""

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}

    def feed(self, data: str) -> list[TextDelta]:
        """Consume one SSE data payload and emit text deltas."""
        try:
            chunk = json.loads(data)
            delta = chunk["choices"][0].get("delta") or {}
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        events: list[TextDelta] = []
        content = delta.get("content")
        if content:
            self._content_parts.append(content)
            events.append(TextDelta(text=content))
        for raw_call in delta.get("tool_calls") or []:
            self._feed_tool_call(raw_call)
        return events

    def _feed_tool_call(self, raw: dict[str, Any]) -> None:
        index = raw.get("index", 0)
        slot = self._tool_calls.setdefault(index, {"id": "", "name": "", "arguments": []})
        if raw.get("id"):
            slot["id"] = raw["id"]
        function = raw.get("function") or {}
        if function.get("name"):
            slot["name"] += function["name"]
        if function.get("arguments"):
            slot["arguments"].append(function["arguments"])

    def build_message(self) -> ChatMessage:
        """Assemble the final assistant message from accumulated deltas."""
        tool_calls = tuple(
            ToolCall(
                id=slot["id"],
                name=slot["name"],
                arguments=self._parse_slot_arguments(slot),
            )
            for _, slot in sorted(self._tool_calls.items())
        )
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="".join(self._content_parts),
            tool_calls=tool_calls,
        )

    @staticmethod
    def _parse_slot_arguments(slot: dict[str, Any]) -> dict[str, Any]:
        raw = "".join(slot["arguments"]) or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        if not isinstance(arguments, dict):
            raise LLMResponseError(PARSE_ERROR_MESSAGE)
        return arguments
