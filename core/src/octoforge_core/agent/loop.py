"""Agent loop: streams events while iterating LLM calls and skill executions."""

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    TextDelta,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import StreamFinished
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.ports import LLMClient
from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.registry import SkillRegistry

MAX_ITERATIONS_MESSAGE = "Agent loop reached the iteration limit"
EMPTY_STREAM_MESSAGE = "LLM stream ended without a final message"
ERROR_OUTPUT_PREFIX = "error: "


@dataclass(slots=True)
class _IterationOutcome:
    """Mutable summary of one streamed LLM call."""

    message: ChatMessage | None = None
    interrupted: bool = False
    failed: bool = False

    def observe(self, event: LoopEvent) -> None:
        if isinstance(event, AssistantMessage):
            self.message = event.message
            self.interrupted = event.interrupted
        elif isinstance(event, Failed):
            self.failed = True


class AgentLoop:
    """Runs the reason-act cycle, emitting events as it goes."""

    def __init__(
        self,
        llm_client: LLMClient,
        registry: SkillRegistry,
        max_iterations: int,
    ) -> None:
        self._llm = llm_client
        self._registry = registry
        self._max_iterations = max_iterations

    def stream(
        self,
        history: list[ChatMessage],
        control: LoopControl,
        context: SkillContext,
    ) -> AsyncIterator[LoopEvent]:
        """Run the loop, yielding events; new messages are appended to history."""
        return self._run(history, control, context)

    async def _run(
        self,
        history: list[ChatMessage],
        control: LoopControl,
        context: SkillContext,
    ) -> AsyncIterator[LoopEvent]:
        for index in range(self._max_iterations):
            yield IterationStarted(index=index)
            history.extend(control.drain())
            if control.is_cancelled:
                yield Cancelled()
                return
            outcome = _IterationOutcome()
            async for event in self._stream_assistant(history, control):
                outcome.observe(event)
                yield event
            if outcome.failed:
                return
            if outcome.interrupted:
                yield Cancelled()
                return
            if outcome.message is None:
                yield Failed(error=EMPTY_STREAM_MESSAGE)
                return
            if not outcome.message.tool_calls:
                yield Finished(message=outcome.message)
                return
            async for event in self._stream_tool_calls(
                outcome.message.tool_calls, context, history
            ):
                yield event
        yield Failed(error=MAX_ITERATIONS_MESSAGE)

    async def _stream_assistant(
        self,
        history: list[ChatMessage],
        control: LoopControl,
    ) -> AsyncIterator[LoopEvent]:
        """Stream one LLM call and append the assistant message to history."""
        stream = self._llm.stream(history, tools=self._registry.specs())
        content_parts: list[str] = []
        final_message: ChatMessage | None = None
        interrupted = False
        async for event in stream:
            if control.is_cancelled:
                interrupted = True
                break
            if isinstance(event, LlmTextDelta):
                content_parts.append(event.text)
                yield TextDelta(text=event.text)
            elif isinstance(event, StreamFinished):
                final_message = event.message
        if isinstance(stream, AsyncGenerator):
            await stream.aclose()
        if interrupted:
            message = ChatMessage(role=MessageRole.ASSISTANT, content="".join(content_parts))
            history.append(message)
            yield AssistantMessage(message=message, interrupted=True)
            return
        if final_message is None:
            yield Failed(error=EMPTY_STREAM_MESSAGE)
            return
        history.append(final_message)
        yield AssistantMessage(message=final_message)

    async def _stream_tool_calls(
        self,
        tool_calls: tuple[ToolCall, ...],
        context: SkillContext,
        history: list[ChatMessage],
    ) -> AsyncIterator[LoopEvent]:
        """Execute tool calls sequentially, appending tool messages to history."""
        for call in tool_calls:
            yield ToolCallRequested(call=call)
            content, error = await self._execute_one(call, context)
            if error is not None:
                yield ToolCallFailed(call=call, error=error)
            else:
                yield ToolCallCompleted(call=call, output=content)
            history.append(
                ChatMessage(role=MessageRole.TOOL, content=content, tool_call_id=call.id)
            )

    async def _execute_one(self, call: ToolCall, context: SkillContext) -> tuple[str, str | None]:
        """Execute one call; failures become error output for the LLM."""
        try:
            skill = self._registry.get(call.name)
            return await skill.execute(call.arguments, context), None
        except Exception as exc:  # skill failures are reported back to the LLM
            error = format_error(exc)
            return f"{ERROR_OUTPUT_PREFIX}{error}", error


def format_error(exc: Exception) -> str:
    """Render an exception for the LLM/user, prefixed with its class name.

    Some exceptions (e.g. httpx timeouts) have an empty `str()` — the class
    name is then the only usable signal.
    """
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__
