"""Tariff checks and usage ledger writes for a dialog actor."""

import logging
from typing import TYPE_CHECKING

from octoforge_core.agent.events import Finished, LoopEvent
from octoforge_core.llm.usage import Usage
from octoforge_core.tariffs.api import LimitVerdict, UsageEvent, UsageKind, UsageOrigin

from .runner_process import Process

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class RunnerUsage:
    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    async def run_budget_verdict(self) -> LimitVerdict | None:
        limits = self._runner._config.limits
        if limits is None:
            return None
        verdict = await limits.check_run_budget(self._runner.user_id)
        return None if verdict.allowed else verdict

    async def record_user_message(self) -> None:
        limits = self._runner._config.limits
        if limits is None:
            return
        await limits.record(
            UsageEvent(
                user_id=self._runner.user_id,
                kind=UsageKind.USER_MESSAGE,
                origin=UsageOrigin.INTERACTIVE,
                quantity=1,
                dialog_id=self._runner.dialog_id,
            )
        )

    async def record_routing(self, usage: Usage | None) -> None:
        limits = self._runner._config.limits
        if limits is None or usage is None:
            return
        await limits.record(
            UsageEvent(
                user_id=self._runner.user_id,
                kind=UsageKind.LLM_ROUTING,
                origin=UsageOrigin.INTERACTIVE,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                dialog_id=self._runner.dialog_id,
            )
        )

    async def record_run(self, process: Process, terminal: LoopEvent) -> None:
        limits = self._runner._config.limits
        if limits is None:
            return
        answered = isinstance(terminal, Finished) and bool(terminal.message.content.strip())
        if not answered and process.spent_prompt == 0 and process.spent_completion == 0:
            return
        await limits.record(
            UsageEvent(
                user_id=self._runner.user_id,
                kind=UsageKind.LLM_ANSWER,
                origin=process.origin,
                prompt_tokens=process.spent_prompt,
                completion_tokens=process.spent_completion,
                quantity=1 if answered else 0,
                dialog_id=self._runner.dialog_id,
                exchange_id=process.exchange_id,
                task_id=process.task_id,
            )
        )
