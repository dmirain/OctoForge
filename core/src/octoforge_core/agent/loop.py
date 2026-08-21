"""Public reason-act loop coordinating assistant turns and eager tool runs."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import Cancelled, Failed, Finished, IterationStarted, LoopEvent
from octoforge_core.agent.loop_assistant import AssistantTurn, AssistantTurnRequest
from octoforge_core.agent.loop_tool_execution import format_error
from octoforge_core.agent.loop_tools import ToolRunTracker as _ToolRunTracker
from octoforge_core.agent.loop_types import (
    CANCELLED_OUTPUT,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    EMPTY_STREAM_MESSAGE,
    ERROR_OUTPUT_PREFIX,
    MAX_ITERATIONS_MESSAGE,
    STREAM_IDLE_TIMEOUT_MESSAGE,
    TOOL_TIMEOUT_MESSAGE,
    IterationOutcome,
)
from octoforge_core.domain import ChatMessage
from octoforge_core.ports import LLMClient
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.registry import ToolRegistry

__all__ = [
    "CANCELLED_OUTPUT",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "EMPTY_STREAM_MESSAGE",
    "ERROR_OUTPUT_PREFIX",
    "MAX_ITERATIONS_MESSAGE",
    "STREAM_IDLE_TIMEOUT_MESSAGE",
    "TOOL_TIMEOUT_MESSAGE",
    "AgentLoop",
    "AgentLoopConfig",
    "format_error",
]


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    max_iterations: int
    stream_idle_timeout: float | None = None
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS


class AgentLoop:
    """Run the reason-act cycle and append its messages to supplied history."""

    def __init__(
        self,
        llm_client: LLMClient,
        registry: ToolRegistry,
        config: AgentLoopConfig,
    ) -> None:
        self._registry = registry
        self._config = config
        self._assistant = AssistantTurn(llm_client, config.stream_idle_timeout)

    def stream(
        self,
        history: list[ChatMessage],
        control: LoopControl,
        context: ToolContext,
    ) -> AsyncIterator[LoopEvent]:
        return self._run(history, control, context)

    async def _run(
        self,
        history: list[ChatMessage],
        control: LoopControl,
        context: ToolContext,
    ) -> AsyncIterator[LoopEvent]:
        specs = self._registry.specs(context)
        for index in range(self._config.max_iterations):
            yield IterationStarted(index=index)
            if control.is_cancelled:
                yield Cancelled()
                return
            outcome = IterationOutcome()
            tracker = _ToolRunTracker(self._registry, context, self._config.tool_timeout)
            request = AssistantTurnRequest(history, control, tracker, specs)
            async for event in self._assistant.stream(request):
                outcome.observe(event)
                yield event
            if outcome.failed:
                return
            if outcome.interrupted:
                yield Cancelled()
                return
            assert outcome.message is not None
            if not outcome.message.tool_calls:
                yield Finished(message=outcome.message, usage=outcome.usage)
                return
            async for event in tracker.finish(outcome.message, history, control):
                yield event
        yield Failed(error=MAX_ITERATIONS_MESSAGE)
