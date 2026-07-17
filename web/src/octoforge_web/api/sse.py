"""Serialization of conversation events to SSE frames."""

import json
from typing import Any

from octoforge_core import LoopEvent
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
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
    """Convert a conversation event into a JSON-serializable payload."""
    base: dict[str, Any] = {"seq": event.seq, "conversation_id": event.conversation_id}
    return base | _event_details(event.payload)


def _event_details(payload: LoopEvent) -> dict[str, Any]:
    """Build the type-tagged payload body for one loop event."""
    if isinstance(payload, IterationStarted):
        details: dict[str, Any] = {"type": "iteration_started", "index": payload.index}
    elif isinstance(payload, TextDelta):
        details = {"type": "text_delta", "text": payload.text}
    elif isinstance(payload, AssistantMessage):
        details = {
            "type": "assistant_message",
            "role": payload.message.role.value,
            "content": payload.message.content,
            "interrupted": payload.interrupted,
        }
    elif isinstance(payload, ToolCallRequested):
        details = {
            "type": "tool_call_requested",
            "name": payload.call.name,
            "arguments": payload.call.arguments,
        }
    elif isinstance(payload, ToolCallCompleted):
        details = {
            "type": "tool_call_completed",
            "name": payload.call.name,
            "output": payload.output,
        }
    elif isinstance(payload, ToolCallFailed):
        details = {"type": "tool_call_failed", "name": payload.call.name, "error": payload.error}
    elif isinstance(payload, Finished):
        details = {"type": "finished", "content": payload.message.content}
    elif isinstance(payload, Cancelled):
        details = {"type": "cancelled"}
    elif isinstance(payload, Failed):
        details = {"type": "failed", "error": payload.error}
    else:
        raise ValueError(UNKNOWN_EVENT_MESSAGE)
    return details
