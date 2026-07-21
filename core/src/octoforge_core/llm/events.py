"""Events produced by streaming LLM completions."""

from dataclasses import dataclass

from octoforge_core.domain import ChatMessage, ToolCall
from octoforge_core.llm.usage import Usage


@dataclass(frozen=True, slots=True)
class TextDelta:
    """An incremental piece of the assistant message."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """A tool call slot appeared in the stream (id and name are known)."""

    index: int
    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolCallReady:
    """A tool call's arguments finished streaming and parsed cleanly."""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolCallBroken:
    """A tool call's arguments did not parse into a JSON object."""

    index: int
    call_id: str
    name: str
    error: str
    raw: str


@dataclass(frozen=True, slots=True)
class StreamFinished:
    """Terminal event carrying the complete assistant message and its usage."""

    message: ChatMessage
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class RetryScheduled:
    """The client is about to retry a transiently failed call."""

    attempt: int
    delay_seconds: float
    reason: str


StreamEvent = (
    TextDelta | ToolCallStarted | ToolCallReady | ToolCallBroken | StreamFinished | RetryScheduled
)
