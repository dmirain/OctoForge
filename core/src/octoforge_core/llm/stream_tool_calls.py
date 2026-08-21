"""State machine for streamed, fragmented tool calls."""

import json
from dataclasses import dataclass, field
from typing import Any

from octoforge_core.domain import ToolCall
from octoforge_core.llm.events import (
    StreamEvent,
    ToolCallBroken,
    ToolCallReady,
    ToolCallStarted,
)
from octoforge_core.llm.openai_wire import ARGUMENTS_NOT_OBJECT_MESSAGE


@dataclass(slots=True)
class ToolCallSlot:
    call_id: str = ""
    name: str = ""
    argument_parts: list[str] = field(default_factory=list)
    arguments: dict[str, Any] = field(default_factory=dict)
    started: bool = False
    closed: bool = False


class StreamToolCalls:
    """Accumulate indexed tool deltas and emit lifecycle events."""

    def __init__(self) -> None:
        self._slots: dict[int, ToolCallSlot] = {}
        self._open_index: int | None = None

    def feed(self, raw: dict[str, Any]) -> list[StreamEvent]:
        index = raw.get("index", 0)
        events: list[StreamEvent] = []
        if self._open_index is not None and index != self._open_index:
            closed = self._close(self._open_index)
            if closed is not None:
                events.append(closed)
        slot = self._slots.setdefault(index, ToolCallSlot())
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
        if self._open_index is None:
            return []
        event = self._close(self._open_index)
        return [event] if event is not None else []

    def calls(self) -> tuple[ToolCall, ...]:
        for index in self._slots:
            self._close(index)
        return tuple(
            ToolCall(slot.call_id, slot.name, slot.arguments)
            for _, slot in sorted(self._slots.items())
        )

    def _close(self, index: int) -> ToolCallReady | ToolCallBroken | None:
        slot = self._slots.get(index)
        if slot is None or slot.closed:
            return None
        slot.closed = True
        raw = "".join(slot.argument_parts) or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ToolCallBroken(index, slot.call_id, slot.name, str(exc), raw)
        if not isinstance(parsed, dict):
            return ToolCallBroken(
                index,
                slot.call_id,
                slot.name,
                ARGUMENTS_NOT_OBJECT_MESSAGE,
                raw,
            )
        slot.arguments = parsed
        return ToolCallReady(ToolCall(slot.call_id, slot.name, parsed))
