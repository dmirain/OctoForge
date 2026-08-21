"""Execute one tool call with bounded lifetime and model-readable failures."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

from octoforge_core.agent.loop_types import ERROR_OUTPUT_PREFIX, TOOL_TIMEOUT_MESSAGE
from octoforge_core.domain import ToolCall
from octoforge_core.tools.base import ToolContext
from octoforge_core.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolRunResult:
    call: ToolCall
    content: str
    error: str | None


class ToolExecutor:
    """Apply lookup, timeout and exception translation to one tool call."""

    def __init__(self, registry: ToolRegistry, context: ToolContext, timeout: float) -> None:
        self._registry = registry
        self._context = context
        self._timeout = timeout

    async def run(self, call: ToolCall) -> ToolRunResult:
        try:
            content = await self._execute(call)
        except ToolTimeoutError as exc:
            error = str(exc)
            return ToolRunResult(call, f"{ERROR_OUTPUT_PREFIX}{error}", error)
        except Exception as exc:
            error = format_error(exc)
            return ToolRunResult(call, f"{ERROR_OUTPUT_PREFIX}{error}", error)
        return ToolRunResult(call, content, None)

    async def _execute(self, call: ToolCall) -> str:
        tool = self._registry.get(call.name)
        task = asyncio.create_task(tool.execute(call.arguments, self._context))
        try:
            done, _pending = await asyncio.wait({task}, timeout=self._timeout)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if done:
            return await task
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        error = TOOL_TIMEOUT_MESSAGE.format(name=call.name, seconds=self._timeout)
        logger.warning("%s", error)
        raise ToolTimeoutError(error)


class ToolTimeoutError(Exception):
    """A tool exceeded the loop's own execution deadline."""


def format_error(exc: Exception) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
