"""OpenAI-compatible chat-completion client."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

import httpx

from octoforge_core.config import LLMConfig
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.errors import TransportError, classify_http_error, raise_for_error_status
from octoforge_core.llm.events import (
    StreamEvent,
    StreamFinished,
    TextDelta,
    ToolCallBroken,
    ToolCallReady,
    ToolCallStarted,
)
from octoforge_core.llm.usage import Completion, Usage, parse_usage
from octoforge_core.tools.base import ToolSpec

CHAT_COMPLETIONS_PATH = "/chat/completions"
PARSE_ERROR_MESSAGE = "Unexpected LLM response payload"
ARGUMENTS_NOT_OBJECT_MESSAGE = "tool call arguments are not a JSON object"
FUNCTION_TOOL_TYPE = "function"
SSE_DATA_PREFIX = "data:"
SSE_DONE_MARKER = "[DONE]"


def _stream_error_status(chunk: dict[str, Any]) -> int:
    """Pick an HTTP status for a mid-stream error payload (numeric code or 500)."""
    error = chunk["error"]
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else HTTPStatus.INTERNAL_SERVER_ERROR


class OpenAICompatibleClient:
    """LLMClient implementation for OpenAI-compatible endpoints."""

    def __init__(self, http_client: httpx.AsyncClient, config: LLMConfig) -> None:
        self._http = http_client
        self._config = config

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        """Call chat/completions (non-streaming) and return the reply."""
        try:
            response = await self._http.post(
                CHAT_COMPLETIONS_PATH,
                json=self._build_payload(messages, tools, stream=False),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        raise_for_error_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        return Completion(message=self._parse_reply(data), usage=parse_usage(data.get("usage")))

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Call chat/completions with streaming and yield events."""
        try:
            async with self._http.stream(
                "POST",
                CHAT_COMPLETIONS_PATH,
                json=self._build_payload(messages, tools, stream=True),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
                timeout=self._config.timeout_seconds,
            ) as response:
                raise_for_error_status(response)
                accumulator = _StreamAccumulator()
                async for line in response.aiter_lines():
                    if not line.startswith(SSE_DATA_PREFIX):
                        continue
                    data = line[len(SSE_DATA_PREFIX) :].strip()
                    if data == SSE_DONE_MARKER:
                        break
                    for event in accumulator.feed(data):
                        yield event
                for event in accumulator.finish():
                    yield event
        except httpx.HTTPError as exc:
            raise TransportError(str(exc) or type(exc).__name__) from exc
        yield StreamFinished(message=accumulator.build_message(), usage=accumulator.usage)

    def _build_payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """Build the chat/completions request payload."""
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": [self._serialize_message(message) for message in messages],
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
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
    def _serialize_tool(spec: ToolSpec) -> dict[str, Any]:
        """Convert a tool spec into an OpenAI function tool."""
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
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        if not isinstance(arguments, dict):
            raise LLMResponseError(ARGUMENTS_NOT_OBJECT_MESSAGE)
        return ToolCall(id=raw["id"], name=raw["function"]["name"], arguments=arguments)


@dataclass(slots=True)
class _ToolCallSlot:
    """Mutable accumulation state of one streamed tool call."""

    call_id: str = ""
    name: str = ""
    argument_parts: list[str] = field(default_factory=list)
    arguments: dict[str, Any] = field(default_factory=dict)
    started: bool = False
    closed: bool = False


class _StreamAccumulator:
    """Accumulates streaming deltas into a message, emitting tool-call events.

    A slot closes when deltas move on to the next `index`; the last open slot
    is closed by `finish()`. Closing emits `ToolCallReady` (arguments parsed)
    or `ToolCallBroken` (unparseable arguments — the call keeps empty
    arguments in the final message instead of failing the stream).
    """

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._tool_calls: dict[int, _ToolCallSlot] = {}
        self._open_index: int | None = None
        self._usage: Usage | None = None

    @property
    def usage(self) -> Usage | None:
        """Token usage reported by the provider, when it sent any."""
        return self._usage

    def feed(self, data: str) -> list[StreamEvent]:
        """Consume one SSE data payload and emit stream events."""
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        if not isinstance(chunk, dict):
            raise LLMResponseError(PARSE_ERROR_MESSAGE)
        if isinstance(chunk.get("error"), dict):
            raise classify_http_error(_stream_error_status(chunk), chunk, retry_after=None)
        usage = parse_usage(chunk.get("usage"))
        if usage is not None:
            self._usage = usage
        choices = chunk.get("choices") or []
        if not choices:
            return []  # usage-only final chunk (stream_options.include_usage)
        try:
            delta = choices[0].get("delta") or {}
        except (IndexError, AttributeError, TypeError) as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        events: list[StreamEvent] = []
        content = delta.get("content")
        if content:
            self._content_parts.append(content)
            events.append(TextDelta(text=content))
        for raw_call in delta.get("tool_calls") or []:
            events.extend(self._feed_tool_call(raw_call))
        return events

    def _feed_tool_call(self, raw: dict[str, Any]) -> list[StreamEvent]:
        index = raw.get("index", 0)
        events: list[StreamEvent] = []
        if self._open_index is not None and index != self._open_index:
            closed = self._close_slot(self._open_index)
            if closed is not None:
                events.append(closed)
        slot = self._tool_calls.setdefault(index, _ToolCallSlot())
        self._open_index = index
        if raw.get("id"):
            slot.call_id = raw["id"]
        function = raw.get("function") or {}
        if function.get("name"):
            slot.name += function["name"]
        if function.get("arguments"):
            slot.argument_parts.append(function["arguments"])
        if not slot.started and slot.call_id and slot.name:
            slot.started = True
            events.append(ToolCallStarted(index=index, call_id=slot.call_id, name=slot.name))
        return events

    def finish(self) -> list[StreamEvent]:
        """Close the last open slot, emitting its terminal tool-call event."""
        if self._open_index is None:
            return []
        event = self._close_slot(self._open_index)
        return [event] if event is not None else []

    def _close_slot(self, index: int) -> ToolCallReady | ToolCallBroken | None:
        slot = self._tool_calls.get(index)
        if slot is None or slot.closed:
            return None
        slot.closed = True
        raw = "".join(slot.argument_parts) or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ToolCallBroken(
                index=index, call_id=slot.call_id, name=slot.name, error=str(exc), raw=raw
            )
        if not isinstance(parsed, dict):
            return ToolCallBroken(
                index=index,
                call_id=slot.call_id,
                name=slot.name,
                error=ARGUMENTS_NOT_OBJECT_MESSAGE,
                raw=raw,
            )
        slot.arguments = parsed
        return ToolCallReady(call=ToolCall(id=slot.call_id, name=slot.name, arguments=parsed))

    def build_message(self) -> ChatMessage:
        """Assemble the final assistant message from accumulated deltas."""
        for index in self._tool_calls:
            self._close_slot(index)
        tool_calls = tuple(
            ToolCall(id=slot.call_id, name=slot.name, arguments=slot.arguments)
            for _, slot in sorted(self._tool_calls.items())
        )
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="".join(self._content_parts),
            tool_calls=tool_calls,
        )
