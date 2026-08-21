"""Serialization of conversation events to SSE frames."""

import json
from collections.abc import Callable
from typing import Any

from octoforge_core import LoopEvent
from octoforge_core.agent.events import (
    AssistantMessage,
    IterationStarted,
    ReasoningDelta,
    TextDelta,
)
from octoforge_core.agent.runner import ConversationEvent

from octoforge_server.api.sse_details import process_event, terminal_event, tool_event

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


_DETAIL_EXTRACTORS: tuple[Callable[[LoopEvent], dict[str, Any] | None], ...] = (
    _stream_event_details,
    tool_event,
    terminal_event,
    process_event,
)
