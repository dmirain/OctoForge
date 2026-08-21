"""Type-specific payload bodies for SSE events."""

from typing import Any

from octoforge_core import LoopEvent
from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    ProcessCompleted,
    ProcessStarted,
    RetryScheduled,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)


def tool_event(payload: LoopEvent) -> dict[str, Any] | None:
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


def terminal_event(payload: LoopEvent) -> dict[str, Any] | None:
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


def process_event(payload: LoopEvent) -> dict[str, Any] | None:
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
