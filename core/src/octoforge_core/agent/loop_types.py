"""Shared state and constants for one agent-loop iteration."""

from dataclasses import dataclass, field

from octoforge_core.agent.events import AssistantMessage, Failed, LoopEvent
from octoforge_core.domain import ChatMessage
from octoforge_core.llm.usage import Usage

MAX_ITERATIONS_MESSAGE = "Agent loop reached the iteration limit"
EMPTY_STREAM_MESSAGE = "LLM stream ended without a final message"
STREAM_IDLE_TIMEOUT_MESSAGE = "LLM stream idle timeout"
ERROR_OUTPUT_PREFIX = "error: "
CANCELLED_OUTPUT = "cancelled"
DEFAULT_TOOL_TIMEOUT_SECONDS = 180.0
TOOL_TIMEOUT_MESSAGE = "tool {name!r} exceeded its {seconds:.0f}s time limit"


class RunCancelledError(Exception):
    """Internal signal raised when cancellation wins the stream-event race."""


@dataclass(slots=True)
class IterationOutcome:
    message: ChatMessage | None = None
    interrupted: bool = False
    failed: bool = False
    usage: Usage | None = None

    def observe(self, event: LoopEvent) -> None:
        if isinstance(event, AssistantMessage):
            self.message = event.message
            self.interrupted = event.interrupted
            self.usage = event.usage
        elif isinstance(event, Failed):
            self.failed = True


@dataclass(slots=True)
class AssistantStreamState:
    content_parts: list[str] = field(default_factory=list)
    final_message: ChatMessage | None = None
    interrupted: bool = False
    timed_out: bool = False
    usage: Usage | None = None
