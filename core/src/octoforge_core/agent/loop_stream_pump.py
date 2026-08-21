"""Consume an LLM stream under cancellation and idle-timeout policy."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import LoopEvent
from octoforge_core.agent.loop_stream_events import translate_stream_event
from octoforge_core.agent.loop_tools import ToolRunTracker
from octoforge_core.agent.loop_types import AssistantStreamState, RunCancelledError
from octoforge_core.llm.events import StreamEvent


@dataclass(frozen=True, slots=True)
class StreamSession:
    stream: AsyncIterator[StreamEvent]
    control: LoopControl
    tracker: ToolRunTracker
    state: AssistantStreamState


class AssistantStreamPump:
    """Race stream progress against cancellation and provider silence."""

    def __init__(self, idle_timeout: float | None) -> None:
        self._idle_timeout = idle_timeout

    async def pump(self, session: StreamSession) -> AsyncIterator[LoopEvent]:
        cancel_watch = asyncio.create_task(session.control.wait_cancelled())
        try:
            while True:
                try:
                    event = await self._next_event(session.stream, cancel_watch)
                except TimeoutError:
                    session.state.timed_out = True
                    return
                except RunCancelledError:
                    session.state.interrupted = True
                    return
                if event is None:
                    return
                if session.control.is_cancelled:
                    session.state.interrupted = True
                    return
                for loop_event in translate_stream_event(event, session.state, session.tracker):
                    yield loop_event
        finally:
            cancel_watch.cancel()

    async def _next_event(
        self,
        stream: AsyncIterator[StreamEvent],
        cancel_watch: asyncio.Task[None],
    ) -> StreamEvent | None:
        stream_task: asyncio.Task[StreamEvent] = asyncio.ensure_future(anext(stream))
        try:
            done, _ = await asyncio.wait(
                {stream_task, cancel_watch},
                timeout=self._idle_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            stream_task.cancel()
            raise
        if stream_task in done:
            try:
                return stream_task.result()
            except StopAsyncIteration:
                return None
        stream_task.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await stream_task
        if cancel_watch in done:
            raise RunCancelledError
        raise TimeoutError
