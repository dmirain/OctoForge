"""Agent loop: streams events while iterating LLM calls and tool executions."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    RetryScheduled,
    TextDelta,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import RetryScheduled as LlmRetryScheduled
from octoforge_core.llm.events import (
    StreamEvent,
    StreamFinished,
    ToolCallBroken,
    ToolCallReady,
)
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.llm.usage import Usage
from octoforge_core.ports import LLMClient
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.registry import ToolRegistry

MAX_ITERATIONS_MESSAGE = "Agent loop reached the iteration limit"
EMPTY_STREAM_MESSAGE = "LLM stream ended without a final message"
STREAM_IDLE_TIMEOUT_MESSAGE = "LLM stream idle timeout"
ERROR_OUTPUT_PREFIX = "error: "
CANCELLED_OUTPUT = "cancelled"


@dataclass(slots=True)
class _IterationOutcome:
    """Mutable summary of one streamed LLM call."""

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
class _AssistantStreamState:
    """Mutable state of one streamed assistant turn."""

    content_parts: list[str] = field(default_factory=list)
    final_message: ChatMessage | None = None
    interrupted: bool = False
    timed_out: bool = False
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class _ToolRunResult:
    """Outcome of one executed (or rejected) tool call."""

    call: ToolCall
    content: str
    error: str | None


class _ToolRunTracker:
    """Owns eager tool runs of one iteration: spawn, drain, wait, abort.

    Results land in a queue as they become ready; tool messages for the
    history are always built in call order, keeping the transcript
    deterministic under concurrent execution.
    """

    def __init__(self, registry: ToolRegistry, context: ToolContext) -> None:
        self._registry = registry
        self._context = context
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._results: dict[str, _ToolRunResult] = {}
        self._queue: asyncio.Queue[_ToolRunResult] = asyncio.Queue()
        self._order: list[ToolCall] = []
        self.seen = False  # any incremental tool-call event arrived this stream

    def spawn(self, call: ToolCall) -> None:
        """Start executing the call in the background."""
        self.seen = True
        self._order.append(call)
        self._tasks[call.id] = asyncio.create_task(self._run_one(call))

    def mark_broken(self, event: ToolCallBroken) -> None:
        """Record an unparseable call: no execution, error reported to the LLM."""
        self.seen = True
        call = ToolCall(id=event.call_id, name=event.name, arguments={})
        self._order.append(call)
        self._queue.put_nowait(
            _ToolRunResult(
                call=call,
                content=f"{ERROR_OUTPUT_PREFIX}{event.error}",
                error=event.error,
            )
        )

    def drain(self) -> list[LoopEvent]:
        """Collect landed results into completion events (non-blocking)."""
        events: list[LoopEvent] = []
        while True:
            try:
                result = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return events
            self._results[result.call.id] = result
            if result.error is not None:
                events.append(ToolCallFailed(call=result.call, error=result.error))
            else:
                events.append(ToolCallCompleted(call=result.call, output=result.content))

    async def wait(self) -> AsyncIterator[LoopEvent]:
        """Yield completion events as the remaining runs finish."""
        pending = {task for task in self._tasks.values() if not task.done()}
        while pending:
            _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for event in self.drain():
                yield event
        for event in self.drain():
            yield event

    async def abort(self) -> None:
        """Cancel running tools, keeping the results that already landed."""
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self.drain()

    def ordered_calls(self) -> tuple[ToolCall, ...]:
        """Calls accepted so far, in arrival order."""
        return tuple(self._order)

    def tool_messages(self, calls: tuple[ToolCall, ...]) -> list[ChatMessage]:
        """Tool replies in call order; a missing result means cancellation."""
        return [
            ChatMessage(
                role=MessageRole.TOOL,
                content=self._result_content(call),
                tool_call_id=call.id,
            )
            for call in calls
        ]

    def _result_content(self, call: ToolCall) -> str:
        result = self._results.get(call.id)
        if result is None:
            return CANCELLED_OUTPUT
        return result.content

    async def _run_one(self, call: ToolCall) -> None:
        content, error = await self._execute_one(call)
        self._queue.put_nowait(_ToolRunResult(call=call, content=content, error=error))

    async def _execute_one(self, call: ToolCall) -> tuple[str, str | None]:
        """Execute one call; failures become error output for the LLM."""
        try:
            tool = self._registry.get(call.name)
            return await tool.execute(call.arguments, self._context), None
        except Exception as exc:  # tool failures are reported back to the LLM
            error = format_error(exc)
            return f"{ERROR_OUTPUT_PREFIX}{error}", error


class AgentLoop:
    """Runs the reason-act cycle, emitting events as it goes."""

    def __init__(
        self,
        llm_client: LLMClient,
        registry: ToolRegistry,
        max_iterations: int,
        stream_idle_timeout: float | None = None,
    ) -> None:
        self._llm = llm_client
        self._registry = registry
        self._max_iterations = max_iterations
        self._stream_idle_timeout = stream_idle_timeout

    def stream(
        self,
        history: list[ChatMessage],
        control: LoopControl,
        context: ToolContext,
    ) -> AsyncIterator[LoopEvent]:
        """Run the loop, yielding events; new messages are appended to history."""
        return self._run(history, control, context)

    async def _run(
        self,
        history: list[ChatMessage],
        control: LoopControl,
        context: ToolContext,
    ) -> AsyncIterator[LoopEvent]:
        specs = self._registry.specs(context)
        for index in range(self._max_iterations):
            yield IterationStarted(index=index)
            history.extend(control.drain())
            if control.is_cancelled:
                yield Cancelled()
                return
            outcome = _IterationOutcome()
            tracker = _ToolRunTracker(self._registry, context)
            async for event in self._stream_assistant(history, control, tracker, specs):
                outcome.observe(event)
                yield event
            if outcome.failed:
                return
            if outcome.interrupted:
                yield Cancelled()
                return
            # a message-less outcome already failed above: the assistant
            # finalizer yields Failed whenever no final message was produced
            assert outcome.message is not None
            if not outcome.message.tool_calls:
                yield Finished(message=outcome.message, usage=outcome.usage)
                return
            async for event in self._stream_tool_calls(outcome.message, tracker, history):
                yield event
        yield Failed(error=MAX_ITERATIONS_MESSAGE)

    async def _stream_assistant(
        self,
        history: list[ChatMessage],
        control: LoopControl,
        tracker: _ToolRunTracker,
        specs: list[ToolSpec],
    ) -> AsyncIterator[LoopEvent]:
        """Stream one LLM call, spawning eager tool runs as calls become ready."""
        stream = self._llm.stream(history, tools=specs)
        state = _AssistantStreamState()
        try:
            async for event in self._pump_stream(stream, control, tracker, state):
                yield event
        except BaseException:
            # the stream died mid-run: abort the eager tool runs of this failed turn
            await tracker.abort()
            raise
        finally:
            if isinstance(stream, AsyncGenerator):
                await stream.aclose()
        async for event in self._finalize_assistant(history, tracker, state):
            yield event

    async def _pump_stream(
        self,
        stream: AsyncIterator[StreamEvent],
        control: LoopControl,
        tracker: _ToolRunTracker,
        state: _AssistantStreamState,
    ) -> AsyncIterator[LoopEvent]:
        """Consume stream events until the end, cancellation, or idle timeout."""
        while True:
            try:
                event = await self._next_event(stream)
            except TimeoutError:
                state.timed_out = True
                return
            if event is None:
                return
            if control.is_cancelled:
                state.interrupted = True
                return
            loop_events, finished, usage = self._handle_stream_event(
                event, state.content_parts, tracker
            )
            if finished is not None:
                state.final_message = finished
                state.usage = usage
            for loop_event in loop_events:
                yield loop_event

    async def _finalize_assistant(
        self,
        history: list[ChatMessage],
        tracker: _ToolRunTracker,
        state: _AssistantStreamState,
    ) -> AsyncIterator[LoopEvent]:
        """Close the turn: fail on timeout, persist partial or final message."""
        if state.timed_out:
            await tracker.abort()
            yield Failed(error=STREAM_IDLE_TIMEOUT_MESSAGE)
            return
        if state.interrupted:
            async for event in self._interrupt_iteration(history, tracker, state.content_parts):
                yield event
            return
        if state.final_message is None:
            yield Failed(error=EMPTY_STREAM_MESSAGE)
            return
        history.append(state.final_message)
        yield AssistantMessage(message=state.final_message, usage=state.usage)

    def _handle_stream_event(
        self,
        event: StreamEvent,
        content_parts: list[str],
        tracker: _ToolRunTracker,
    ) -> tuple[list[LoopEvent], ChatMessage | None, Usage | None]:
        """Translate one LLM stream event into loop events and a final message."""
        events: list[LoopEvent] = []
        final_message: ChatMessage | None = None
        usage: Usage | None = None
        if isinstance(event, LlmTextDelta):
            content_parts.append(event.text)
            events.append(TextDelta(text=event.text))
        elif isinstance(event, ToolCallReady):
            tracker.spawn(event.call)
            events.append(ToolCallRequested(call=event.call))
        elif isinstance(event, ToolCallBroken):
            tracker.mark_broken(event)
        elif isinstance(event, StreamFinished):
            final_message = event.message
            usage = event.usage
        elif isinstance(event, LlmRetryScheduled):
            events.append(
                RetryScheduled(
                    attempt=event.attempt,
                    delay_seconds=event.delay_seconds,
                    reason=event.reason,
                )
            )
        events.extend(tracker.drain())
        return events, final_message, usage

    async def _interrupt_iteration(
        self,
        history: list[ChatMessage],
        tracker: _ToolRunTracker,
        content_parts: list[str],
    ) -> AsyncIterator[LoopEvent]:
        """Abort tool runs and persist the partial message with its call replies."""
        await tracker.abort()
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content="".join(content_parts),
            tool_calls=tracker.ordered_calls(),
        )
        history.append(message)
        history.extend(tracker.tool_messages(message.tool_calls))
        yield AssistantMessage(message=message, interrupted=True)

    async def _next_event(self, stream: AsyncIterator[StreamEvent]) -> StreamEvent | None:
        """Await the next stream event under the idle timeout; None = stream ended."""
        try:
            if self._stream_idle_timeout is None:
                return await anext(stream)
            return await asyncio.wait_for(anext(stream), timeout=self._stream_idle_timeout)
        except StopAsyncIteration:
            return None

    async def _stream_tool_calls(
        self,
        message: ChatMessage,
        tracker: _ToolRunTracker,
        history: list[ChatMessage],
    ) -> AsyncIterator[LoopEvent]:
        """Finish the iteration's tool runs; history follows the call order.

        Streams without incremental tool-call events (mocks, batch providers)
        fall back to spawning every call of the final message here.
        """
        if not tracker.seen:
            for call in message.tool_calls:
                tracker.spawn(call)
                yield ToolCallRequested(call=call)
        async for event in tracker.wait():
            yield event
        history.extend(tracker.tool_messages(message.tool_calls))


def format_error(exc: Exception) -> str:
    """Render an exception for the LLM/user, prefixed with its class name.

    Some exceptions (e.g. httpx timeouts) have an empty `str()` — the class
    name is then the only usable signal.
    """
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__
