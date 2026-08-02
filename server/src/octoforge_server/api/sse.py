"""Serialization of conversation events to SSE frames."""

import json
from collections.abc import Callable
from typing import Any

from octoforge_core import LoopEvent
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    ProcessCompleted,
    ProcessStarted,
    ReasoningDelta,
    RetryScheduled,
    TextDelta,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.agent.runner import ConversationEvent

FRAME_TEMPLATE = "data: {payload}\n\n"
HEARTBEAT_FRAME = ": heartbeat\n\n"
UNKNOWN_EVENT_MESSAGE = "Unknown loop event"


def encode_frame(payload: dict[str, Any]) -> str:
    """Encode a JSON payload as an SSE data frame."""
    return FRAME_TEMPLATE.format(payload=json.dumps(payload, ensure_ascii=False))


def encode_heartbeat() -> str:
    """Return an SSE comment frame used as a heartbeat."""
    return HEARTBEAT_FRAME


def event_to_payload(event: ConversationEvent) -> dict[str, Any]:
    """Convert a conversation event into a JSON-serializable payload.

    `exchange_id` routes the event to its per-exchange bubble: answers of
    different questions stream concurrently, each into its own message.
    """
    base: dict[str, Any] = {
        "seq": event.seq,
        "dialog_id": event.dialog_id,
        "exchange_id": event.exchange_id,
    }
    return base | _event_details(event.payload)


def _event_details(payload: LoopEvent) -> dict[str, Any]:
    """Build the type-tagged payload body for one loop event."""
    for extract in _DETAIL_EXTRACTORS:
        details = extract(payload)
        if details is not None:
            return details
    raise ValueError(UNKNOWN_EVENT_MESSAGE)


def _stream_event_details(payload: LoopEvent) -> dict[str, Any] | None:
    if isinstance(payload, IterationStarted):
        return {"type": "iteration_started", "index": payload.index}
    if isinstance(payload, TextDelta):
        return {"type": "text_delta", "text": payload.text}
    if isinstance(payload, ReasoningDelta):
        return {"type": "reasoning_delta"}
    if isinstance(payload, AssistantMessage):
        return {
            "type": "assistant_message",
            "role": payload.message.role.value,
            "content": payload.message.content,
            "interrupted": payload.interrupted,
        }
    return None


def _tool_event_details(payload: LoopEvent) -> dict[str, Any] | None:
    if isinstance(payload, ToolCallRequested):
        return {
            "type": "tool_call_requested",
            "name": payload.call.name,
            "arguments": payload.call.arguments,
        }
    if isinstance(payload, ToolCallCompleted):
        return {"type": "tool_call_completed", "name": payload.call.name, "output": payload.output}
    if isinstance(payload, ToolCallFailed):
        return {"type": "tool_call_failed", "name": payload.call.name, "error": payload.error}
    return None


def _terminal_event_details(payload: LoopEvent) -> dict[str, Any] | None:
    if isinstance(payload, Finished):
        return {"type": "finished", "content": payload.message.content}
    if isinstance(payload, Cancelled):
        return {"type": "cancelled"}
    if isinstance(payload, Failed):
        return {"type": "failed", "error": payload.error}
    if isinstance(payload, RetryScheduled):
        return {
            "type": "retry_scheduled",
            "attempt": payload.attempt,
            "delay_seconds": payload.delay_seconds,
            "reason": payload.reason,
        }
    return None


def _process_event_details(payload: LoopEvent) -> dict[str, Any] | None:
    if isinstance(payload, ProcessStarted):
        return {
            "type": "process_started",
            "process_id": payload.process_id,
            "title": payload.title,
            "source_client_message_id": payload.source_client_message_id,
        }
    if isinstance(payload, ProcessCompleted):
        return {
            "type": "process_completed",
            "process_id": payload.process_id,
            "title": payload.title,
            "status": payload.status,
        }
    return None


_DETAIL_EXTRACTORS: tuple[Callable[[LoopEvent], dict[str, Any] | None], ...] = (
    _stream_event_details,
    _tool_event_details,
    _terminal_event_details,
    _process_event_details,
)
