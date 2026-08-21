"""Streaming one process loop under its exchange delivery tag."""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.agent.events import (
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
)
from octoforge_core.agent.loop import format_error
from octoforge_core.llm.errors import ContextOverflowError
from octoforge_core.tools.base import ToolContext

from .runner_process import Process, observe_spend
from .runner_text import muted_after_ask
from .runner_tool_ports import DialogImageInspector, DialogUserPrompter

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class ProcessStream:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def terminal(self, process: Process) -> LoopEvent:
        while True:
            try:
                return await self._once(process)
            except ContextOverflowError as exc:
                if process.overflow_retried or not process.narrative_built:
                    return self.fail(process, format_error(exc))
                process.overflow_retried = True
                logger.info(
                    "context overflow, compacting reactively: dialog=%s process=%s",
                    self._runner.dialog_id,
                    process.id,
                )
                compacted = await self._runner._config.compactor.compact_now(
                    self._runner._seed.dialog
                )
                if not compacted:
                    return self.fail(process, format_error(exc))
                await self._runner._context.sync_branch(process, force=True)

    async def _once(self, process: Process) -> LoopEvent:
        context = await self._tool_context(process)
        terminal: LoopEvent = Failed(error="loop ended without a terminal event")
        try:
            async for event in self._runner._config.loop.stream(
                process.branch, process.control, context
            ):
                candidate = await self._consume(process, event)
                if candidate is not None:
                    terminal = candidate
        except ContextOverflowError:
            raise
        except Exception as exc:
            logger.exception(
                "process loop crashed: dialog=%s process=%s",
                self._runner.dialog_id,
                process.id,
            )
            terminal = self.fail(process, format_error(exc))
        return terminal

    async def _consume(self, process: Process, event: LoopEvent) -> LoopEvent | None:
        if isinstance(event, IterationStarted):
            await self._runner._context.sync_branch(process)
        observe_spend(process, event)
        outgoing = self._outgoing(process, event)
        if outgoing is None:
            return None
        accepted = self._send(process, outgoing)
        if isinstance(outgoing, (Finished, Failed)):
            process.terminal_accepted = accepted > 0
        return outgoing if isinstance(outgoing, (Finished, Cancelled, Failed)) else None

    async def _tool_context(self, process: Process) -> ToolContext:
        runtime = self._runner._runtime
        assert runtime.spawner is not None and runtime.deleter is not None
        limits = self._runner._config.limits
        features = await limits.enabled_features(self._runner.user_id) if limits else None
        return ToolContext(
            user_id=self._runner.user_id,
            channel=self._runner.channel,
            dialog_id=self._runner.dialog_id,
            task_spawner=runtime.spawner,
            task_deleter=runtime.deleter,
            user_prompter=DialogUserPrompter(self._runner, process.id),
            image_inspector=(
                DialogImageInspector(self._runner) if self._runner._vision.available else None
            ),
            owner_task_id=process.task_id,
            enabled_features=features,
        )

    def _send(self, process: Process, event: LoopEvent) -> int:
        if process.exchange_id is None:
            return 0
        return self._runner._broadcast(event, process.exchange_id)

    @staticmethod
    def _outgoing(process: Process, event: LoopEvent) -> LoopEvent | None:
        outgoing = event
        if isinstance(event, Finished):
            outgoing = replace(event, source_client_message_id=process.source_client_message_id)
        return muted_after_ask(outgoing) if process.asked else outgoing

    def fail(self, process: Process, error: str) -> Failed:
        terminal = Failed(error=error)
        process.terminal_accepted = self._send(process, terminal) > 0
        return terminal
