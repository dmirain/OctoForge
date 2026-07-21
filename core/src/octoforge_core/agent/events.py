"""Events emitted by the agent loop."""

from dataclasses import dataclass

from octoforge_core.domain import ChatMessage, ToolCall
from octoforge_core.llm.usage import Usage


@dataclass(frozen=True, slots=True)
class IterationStarted:
    """A new reasoning iteration begins."""

    index: int


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A streamed piece of the assistant reply."""

    text: str


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """The completed assistant message of an iteration."""

    message: ChatMessage
    interrupted: bool = False
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class ToolCallRequested:
    """The loop starts executing a tool call."""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """A tool call finished successfully."""

    call: ToolCall
    output: str


@dataclass(frozen=True, slots=True)
class ToolCallFailed:
    """A tool call failed; the error is reported back to the LLM."""

    call: ToolCall
    error: str


@dataclass(frozen=True, slots=True)
class Finished:
    """The loop produced the final answer."""

    message: ChatMessage
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class Cancelled:
    """The run was cancelled by the user."""


@dataclass(frozen=True, slots=True)
class Failed:
    """The loop failed without a final answer."""

    error: str


@dataclass(frozen=True, slots=True)
class RetryScheduled:
    """The LLM client is retrying a transiently failed call."""

    attempt: int
    delay_seconds: float
    reason: str


@dataclass(frozen=True, slots=True)
class ProcessSuspended:
    """Actor marker: the foreground process moved to the background."""

    process_id: str
    title: str


@dataclass(frozen=True, slots=True)
class ProcessResumed:
    """Actor marker: a background process became the foreground."""

    process_id: str
    title: str


@dataclass(frozen=True, slots=True)
class ProcessCompleted:
    """Actor marker: a process reached a terminal status (TaskStatus values)."""

    process_id: str
    title: str
    status: str


LoopEvent = (
    IterationStarted
    | TextDelta
    | AssistantMessage
    | ToolCallRequested
    | ToolCallCompleted
    | ToolCallFailed
    | Finished
    | Cancelled
    | Failed
    | RetryScheduled
    | ProcessSuspended
    | ProcessResumed
    | ProcessCompleted
)
