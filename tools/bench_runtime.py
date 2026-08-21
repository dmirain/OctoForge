"""Scripted LLM and fixed-latency tool used by the benchmark stack."""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from bench_types import TOOL_LATENCY, TOOL_NAME, Marks, Script
from octoforge_core import ChatMessage, Completion, MessageRole, ToolCall, ToolSpec
from octoforge_core.agent.router import ROUTE_TOOL_NAME
from octoforge_core.llm.events import StreamEvent, StreamFinished, ToolCallReady
from octoforge_core.llm.events import TextDelta as LlmTextDelta
from octoforge_core.tools.base import ToolContext


class BenchLLM:
    def __init__(self, scripts: list[Script], marks: Marks) -> None:
        self._scripts = list(scripts)
        self._marks = marks

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> Completion:
        self._marks.routed.append(time.perf_counter())
        call = ToolCall(
            "route-call",
            ROUTE_TOOL_NAME,
            {"action": "new", "exchange_id": None, "cancel_exchange_ids": []},
        )
        return Completion(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=(call,))
        )

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self._marks.request.append(time.perf_counter())
        script = self._scripts.pop(0) if len(self._scripts) > 1 else self._scripts[0]
        calls = tuple(
            ToolCall(f"call-{index}", TOOL_NAME, {})
            for index in range(script.tool_calls)
        )
        for call in calls:
            yield ToolCallReady(call)
        text = ""
        for index in range(script.tokens):
            if script.body_seconds:
                await asyncio.sleep(script.body_seconds / script.tokens)
            piece = f"token{index} "
            text += piece
            self._marks.token_emitted.append(time.perf_counter())
            yield LlmTextDelta(piece)
        self._marks.stream_finished.append(time.perf_counter())
        yield StreamFinished(
            ChatMessage(role=MessageRole.ASSISTANT, content=text, tool_calls=calls)
        )


class WaitTool:
    def __init__(self, marks: Marks) -> None:
        self._marks = marks
        self.spec = ToolSpec(
            TOOL_NAME, "Wait for a fixed benchmark time.", {"type": "object"}
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        self._marks.tool_started.append(time.perf_counter())
        await asyncio.sleep(TOOL_LATENCY)
        self._marks.tool_finished.append(time.perf_counter())
        return "done"
