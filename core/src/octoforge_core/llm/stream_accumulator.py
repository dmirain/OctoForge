"""Accumulate OpenAI SSE deltas into events, usage and a final message."""

import json
from http import HTTPStatus
from typing import Any

from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.errors import LLMResponseError
from octoforge_core.llm.errors import classify_http_error
from octoforge_core.llm.events import ReasoningDelta, StreamEvent, TextDelta
from octoforge_core.llm.openai_wire import PARSE_ERROR_MESSAGE
from octoforge_core.llm.stream_tool_calls import StreamToolCalls
from octoforge_core.llm.usage import Usage, parse_usage

CONSUMED_DELTA_FIELDS = frozenset(
    {"content", "tool_calls", "role", "reasoning", "reasoning_content"}
)


class StreamAccumulator:
    def __init__(self) -> None:
        self._content: list[str] = []
        self._tools = StreamToolCalls()
        self._usage: Usage | None = None
        self._ignored_fields: dict[str, int] = {}

    @property
    def usage(self) -> Usage | None:
        return self._usage

    @property
    def ignored_fields(self) -> dict[str, int]:
        return self._ignored_fields

    def feed(self, data: str) -> list[StreamEvent]:
        chunk = _parse_chunk(data)
        if isinstance(chunk.get("error"), dict):
            raise classify_http_error(_stream_error_status(chunk), chunk, retry_after=None)
        usage = parse_usage(chunk.get("usage"))
        if usage is not None:
            self._usage = usage
        choices = chunk.get("choices") or []
        if not choices:
            return []
        try:
            delta = choices[0].get("delta") or {}
        except (IndexError, AttributeError, TypeError) as exc:
            raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
        self._note_ignored(delta)
        events = self._content_events(delta)
        for raw_call in delta.get("tool_calls") or []:
            events.extend(self._tools.feed(raw_call))
        return events

    def finish(self) -> list[StreamEvent]:
        return self._tools.finish()

    def build_message(self) -> ChatMessage:
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="".join(self._content),
            tool_calls=self._tools.calls(),
        )

    def _content_events(self, delta: dict[str, Any]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        if delta.get("reasoning") or delta.get("reasoning_content"):
            events.append(ReasoningDelta())
        content = delta.get("content")
        if content:
            self._content.append(content)
            events.append(TextDelta(content))
        return events

    def _note_ignored(self, delta: dict[str, Any]) -> None:
        for key, value in delta.items():
            if value and key not in CONSUMED_DELTA_FIELDS:
                self._ignored_fields[key] = self._ignored_fields.get(key, 0) + 1


def _parse_chunk(data: str) -> dict[str, Any]:
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(PARSE_ERROR_MESSAGE) from exc
    if not isinstance(chunk, dict):
        raise LLMResponseError(PARSE_ERROR_MESSAGE)
    return chunk


def _stream_error_status(chunk: dict[str, Any]) -> int:
    error = chunk["error"]
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else HTTPStatus.INTERNAL_SERVER_ERROR
