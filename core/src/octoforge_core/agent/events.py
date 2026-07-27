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
    """The loop produced the final answer.

    `source_client_message_id` is the transport-level id of the user message
    this answer belongs to (the runner enriches it from the answer task);
    None for background/cron results and requeued messages.
    """

    message: ChatMessage
    usage: Usage | None = None
    source_client_message_id: str | None = None


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
class ProcessStarted:
    """Actor marker: the text of this answer is about to begin.

    Broadcast before the first token of a foreground stream and at the head
    of a whole-message outbox delivery, so a transport that threads replies
    knows the target BEFORE it has to create the message (a reply can only be
    set at creation time). `source_client_message_id` mirrors `Finished`'s.
    """

    process_id: str
    title: str
    source_client_message_id: str | None = None


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
    | ProcessStarted
    | ProcessCompleted
)
