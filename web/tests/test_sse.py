"""Unit tests for SSE frame serialization."""

import json

import pytest
from octoforge_core import ChatMessage, LoopEvent, MessageRole, ToolCall
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

from octoforge_web.api.sse import encode_frame, encode_heartbeat, event_to_payload

CONVERSATION_ID = "conv-1"
SEQ = 7
CALL_ID = "call-1"
SKILL_NAME = "http_request"
OUTPUT = "skill output"
ERROR = "boom"
CONTENT = "answer"
ITERATION_INDEX = 2

ASSISTANT = ChatMessage(role=MessageRole.ASSISTANT, content=CONTENT)
CALL = ToolCall(id=CALL_ID, name=SKILL_NAME, arguments={"q": 1})

PAYLOAD_CASES: list[tuple[LoopEvent, dict[str, object]]] = [
    (
        IterationStarted(index=ITERATION_INDEX),
        {"type": "iteration_started", "index": ITERATION_INDEX},
    ),
    (TextDelta(text=CONTENT), {"type": "text_delta", "text": CONTENT}),
    (
        AssistantMessage(message=ASSISTANT, interrupted=True),
        {
            "type": "assistant_message",
            "role": "assistant",
            "content": CONTENT,
            "interrupted": True,
        },
    ),
    (
        ToolCallRequested(call=CALL),
        {"type": "tool_call_requested", "name": SKILL_NAME, "arguments": {"q": 1}},
    ),
    (
        ToolCallCompleted(call=CALL, output=OUTPUT),
        {"type": "tool_call_completed", "name": SKILL_NAME, "output": OUTPUT},
    ),
    (
        ToolCallFailed(call=CALL, error=ERROR),
        {"type": "tool_call_failed", "name": SKILL_NAME, "error": ERROR},
    ),
    (Finished(message=ASSISTANT), {"type": "finished", "content": CONTENT}),
    (Cancelled(), {"type": "cancelled"}),
    (Failed(error=ERROR), {"type": "failed", "error": ERROR}),
]


def make_envelope(payload: LoopEvent) -> ConversationEvent:
    return ConversationEvent(conversation_id=CONVERSATION_ID, seq=SEQ, payload=payload)


@pytest.mark.parametrize(("event", "expected"), PAYLOAD_CASES)
def test_event_to_payload(event: LoopEvent, expected: dict[str, object]) -> None:
    payload = event_to_payload(make_envelope(event))

    assert payload == {
        "seq": SEQ,
        "conversation_id": CONVERSATION_ID,
        **expected,
    }


def test_event_to_payload_rejects_unknown() -> None:
    envelope = ConversationEvent(
        conversation_id=CONVERSATION_ID,
        seq=SEQ,
        payload="not-an-event",  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="Unknown loop event"):
        event_to_payload(envelope)


def test_encode_frame_format() -> None:
    frame = encode_frame({"type": "cancelled"})

    assert frame == 'data: {"type": "cancelled"}\n\n'


def test_encode_frame_keeps_unicode() -> None:
    frame = encode_frame({"text": "привет"})

    assert "привет" in frame
    assert json.loads(frame[len("data: ") :]) == {"text": "привет"}


def test_encode_heartbeat() -> None:
    assert encode_heartbeat() == ": heartbeat\n\n"
