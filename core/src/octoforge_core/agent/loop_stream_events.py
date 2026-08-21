"""Translate provider stream events into loop events and turn state."""

from octoforge_core.agent.events import (
    LoopEvent,
    ReasoningDelta,
    RetryScheduled,
    TextDelta,
    ToolCallRequested,
)
from octoforge_core.agent.loop_tools import ToolRunTracker
from octoforge_core.agent.loop_types import AssistantStreamState
from octoforge_core.llm.events import ReasoningDelta as LlmReasoningDelta
from octoforge_core.llm.events import RetryScheduled as LlmRetryScheduled
from octoforge_core.llm.events import StreamEvent, StreamFinished, ToolCallBroken, ToolCallReady
from octoforge_core.llm.events import TextDelta as LlmTextDelta


def translate_stream_event(
    event: StreamEvent,
    state: AssistantStreamState,
    tracker: ToolRunTracker,
) -> list[LoopEvent]:
    events: list[LoopEvent] = []
    if isinstance(event, LlmTextDelta):
        state.content_parts.append(event.text)
        events.append(TextDelta(text=event.text))
    elif isinstance(event, LlmReasoningDelta):
        events.append(ReasoningDelta())
    elif isinstance(event, ToolCallReady):
        tracker.spawn(event.call)
        events.append(ToolCallRequested(call=event.call))
    elif isinstance(event, ToolCallBroken):
        tracker.mark_broken(event)
    elif isinstance(event, StreamFinished):
        state.final_message = event.message
        state.usage = event.usage
    elif isinstance(event, LlmRetryScheduled):
        events.append(RetryScheduled(event.attempt, event.delay_seconds, event.reason))
    events.extend(tracker.drain())
    return events
