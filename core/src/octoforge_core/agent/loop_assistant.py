"""Run and finalize one streamed assistant turn."""

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import AssistantMessage, Failed, LoopEvent
from octoforge_core.agent.loop_stream_pump import AssistantStreamPump, StreamSession
from octoforge_core.agent.loop_tools import ToolRunTracker
from octoforge_core.agent.loop_types import (
    EMPTY_STREAM_MESSAGE,
    STREAM_IDLE_TIMEOUT_MESSAGE,
    AssistantStreamState,
)
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.ports import LLMClient
from octoforge_core.tools.base import ToolSpec


@dataclass(frozen=True, slots=True)
class AssistantTurnRequest:
    history: list[ChatMessage]
    control: LoopControl
    tracker: ToolRunTracker
    specs: list[ToolSpec]


class AssistantTurn:
    """Own provider streaming, eager-run cleanup and transcript finalization."""

    def __init__(self, llm: LLMClient, idle_timeout: float | None) -> None:
        self._llm = llm
        self._pump = AssistantStreamPump(idle_timeout)

    async def stream(self, request: AssistantTurnRequest) -> AsyncIterator[LoopEvent]:
        stream = self._llm.stream(request.history, tools=request.specs)
        state = AssistantStreamState()
        try:
            session = StreamSession(stream, request.control, request.tracker, state)
            async for event in self._pump.pump(session):
                yield event
        except BaseException:
            await request.tracker.abort()
            raise
        finally:
            if isinstance(stream, AsyncGenerator):
                await stream.aclose()
        async for event in self._finalize(request, state):
            yield event

    async def _finalize(
        self,
        request: AssistantTurnRequest,
        state: AssistantStreamState,
    ) -> AsyncIterator[LoopEvent]:
        if state.timed_out:
            await request.tracker.abort()
            yield Failed(error=STREAM_IDLE_TIMEOUT_MESSAGE)
            return
        if state.interrupted:
            await request.tracker.abort()
            message = ChatMessage(
                MessageRole.ASSISTANT,
                "".join(state.content_parts),
                tool_calls=request.tracker.ordered_calls(),
            )
            request.history.append(message)
            request.history.extend(request.tracker.tool_messages(message.tool_calls))
            yield AssistantMessage(message=message, interrupted=True)
            return
        if state.final_message is None:
            yield Failed(error=EMPTY_STREAM_MESSAGE)
            return
        request.history.append(state.final_message)
        yield AssistantMessage(message=state.final_message, usage=state.usage)
