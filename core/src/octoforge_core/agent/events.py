"""Events emitted by the agent loop."""

from dataclasses import dataclass

from octoforge_core.agent.process_events import ProcessCompleted as _ProcessCompleted
from octoforge_core.agent.process_events import ProcessStarted as _ProcessStarted
from octoforge_core.domain import ChatMessage, ToolCall
from octoforge_core.llm.usage import Usage

ProcessCompleted = _ProcessCompleted
ProcessStarted = _ProcessStarted


@dataclass(frozen=True, slots=True)
class IterationStarted:
    """A new reasoning iteration begins."""

    index: int


@dataclass(frozen=True, slots=True)
class TextDelta:
    """A streamed piece of the assistant reply."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """The model is thinking: one chunk of hidden reasoning arrived.

    Carries no text (the reasoning is the model's scratchpad, never shown or
    stored); surfaces render the state — and count the chunks as progress —
    so a long think does not look like the bot dying mid-answer.
    """


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


LoopEvent = (
    IterationStarted
    | TextDelta
    | ReasoningDelta
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
