"""A tool that never returns must not hold its dialog open forever.

The loop awaits every eager tool run before it can finish a turn, so one wedged
call freezes that dialog until the process restarts — the same failure the LLM
stream already had an idle timeout for, and tools did not.
"""

import asyncio
from typing import Any

import pytest

from octoforge_core.agent.loop import ERROR_OUTPUT_PREFIX, _ToolRunTracker
from octoforge_core.domain import ToolCall
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.registry import ToolRegistry

TIMEOUT = 0.05
PATIENCE = TIMEOUT * 40
CALL_ID = "call-1"


class HangingTool:
    """Never returns — how a wedged HTTP call behaves from the loop's side."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="hangs", description="never returns", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class QuickTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="quick", description="returns at once", parameters_schema={})

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        return "done"


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(user_id="user-1", channel="web", dialog_id="dialog-1")


def tracker_for(tool: object, context: ToolContext) -> _ToolRunTracker:
    registry = ToolRegistry()
    registry.register(tool)
    return _ToolRunTracker(registry, context, timeout_seconds=TIMEOUT)


async def drain_all(tracker: _ToolRunTracker) -> None:
    async for _event in tracker.wait():
        pass


async def test_a_wedged_tool_becomes_error_output_rather_than_a_hang(
    context: ToolContext,
) -> None:
    """Without the timeout this waits forever; `PATIENCE` is what proves it does not."""
    tracker = tracker_for(HangingTool(), context)
    call = ToolCall(id=CALL_ID, name="hangs", arguments={})
    tracker.spawn(call)

    await asyncio.wait_for(drain_all(tracker), timeout=PATIENCE)

    (message,) = tracker.tool_messages((call,))
    assert message.content.startswith(ERROR_OUTPUT_PREFIX)
    assert "time limit" in message.content


async def test_the_model_is_told_which_tool_ran_out_of_time(context: ToolContext) -> None:
    """Reported as ordinary tool output, so the model can try something else
    instead of the whole run failing."""
    tracker = tracker_for(HangingTool(), context)
    call = ToolCall(id=CALL_ID, name="hangs", arguments={})
    tracker.spawn(call)

    await asyncio.wait_for(drain_all(tracker), timeout=PATIENCE)

    (message,) = tracker.tool_messages((call,))
    assert "hangs" in message.content


async def test_a_tool_that_answers_in_time_is_untouched(context: ToolContext) -> None:
    tracker = tracker_for(QuickTool(), context)
    call = ToolCall(id=CALL_ID, name="quick", arguments={})
    tracker.spawn(call)

    await drain_all(tracker)

    (message,) = tracker.tool_messages((call,))
    assert message.content == "done"
