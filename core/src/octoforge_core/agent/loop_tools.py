"""Track eager tool runs and preserve deterministic transcript order."""

import asyncio
from collections.abc import AsyncIterator

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    LoopEvent,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.agent.loop_tool_execution import ToolExecutor, ToolRunResult
from octoforge_core.agent.loop_types import CANCELLED_OUTPUT, ERROR_OUTPUT_PREFIX
from octoforge_core.domain import ChatMessage, MessageRole, ToolCall
from octoforge_core.llm.events import ToolCallBroken
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.registry import ToolRegistry


class ToolRunTracker:
    """Own eager runs, completion events and call-ordered tool messages."""

    def __init__(
        self, registry: ToolRegistry, context: ToolContext, timeout_seconds: float
    ) -> None:
        self._executor = ToolExecutor(registry, context, timeout_seconds)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._results: dict[str, ToolRunResult] = {}
        self._queue: asyncio.Queue[ToolRunResult] = asyncio.Queue()
        self._order: list[ToolCall] = []
        self.seen = False

    def spawn(self, call: ToolCall) -> None:
        self.seen = True
        self._order.append(call)
        self._tasks[call.id] = asyncio.create_task(self._run_one(call))

    def mark_broken(self, event: ToolCallBroken) -> None:
        self.seen = True
        call = ToolCall(id=event.call_id, name=event.name, arguments={})
        self._order.append(call)
        self._queue.put_nowait(
            ToolRunResult(call, f"{ERROR_OUTPUT_PREFIX}{event.error}", event.error)
        )

    def drain(self) -> list[LoopEvent]:
        events: list[LoopEvent] = []
        while True:
            try:
                result = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return events
            self._results[result.call.id] = result
            if result.error is None:
                events.append(ToolCallCompleted(call=result.call, output=result.content))
            else:
                events.append(ToolCallFailed(call=result.call, error=result.error))

    async def wait(
        self, cancel_watch: asyncio.Task[None] | None = None
    ) -> AsyncIterator[LoopEvent]:
        pending = {task for task in self._tasks.values() if not task.done()}
        while pending:
            wait_set = pending | ({cancel_watch} if cancel_watch is not None else set())
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
            if cancel_watch is not None and cancel_watch in done:
                return
            pending -= done
            for event in self.drain():
                yield event
        for event in self.drain():
            yield event

    async def finish(
        self, message: ChatMessage, history: list[ChatMessage], control: LoopControl
    ) -> AsyncIterator[LoopEvent]:
        if not self.seen:
            for call in message.tool_calls:
                self.spawn(call)
                yield ToolCallRequested(call=call)
        cancel_watch = asyncio.create_task(control.wait_cancelled())
        try:
            async for event in self.wait(cancel_watch):
                yield event
        finally:
            cancel_watch.cancel()
        if control.is_cancelled:
            await self.abort()
        history.extend(self.tool_messages(message.tool_calls))

    async def abort(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self.drain()

    def ordered_calls(self) -> tuple[ToolCall, ...]:
        return tuple(self._order)

    def tool_messages(self, calls: tuple[ToolCall, ...]) -> list[ChatMessage]:
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
        return CANCELLED_OUTPUT if result is None else result.content

    async def _run_one(self, call: ToolCall) -> None:
        self._queue.put_nowait(await self._executor.run(call))
