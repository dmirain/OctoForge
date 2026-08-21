"""Branch assembly and compacted hot-tail bookkeeping."""

import asyncio
import logging
from typing import TYPE_CHECKING

from octoforge_core.agent.branch import render_branch
from octoforge_core.agent.prompts import SYSTEM_PROMPT_NAME
from octoforge_core.dialogs.api import MessageAppend
from octoforge_core.domain import ChatMessage, MessageRole
from octoforge_core.llm.usage import Usage

from .runner_process import Process
from .runner_text import with_date_envelope

if TYPE_CHECKING:
    from .runner import ConversationRunner

logger = logging.getLogger(__name__)


class NarrativeContext:
    """Builds process branches from durable obligations and the compacted tail."""

    def __init__(self, runner: "ConversationRunner") -> None:
        self._runner = runner

    def system_message(self) -> ChatMessage:
        prompt = self._runner._config.prompts.get(SYSTEM_PROMPT_NAME)
        return ChatMessage(role=MessageRole.SYSTEM, content=prompt)

    async def assemble(self, own_exchange_id: str | None = None) -> tuple[list[ChatMessage], int]:
        runtime = self._runner._runtime
        async with runtime.assemble_lock:
            assembled, live_ids = await asyncio.gather(
                self._runner._config.compactor.assemble(
                    self._runner._seed.dialog, runtime.narrative
                ),
                self.live_exchange_ids(own_exchange_id),
            )
            self.trim(assembled.snapshot_len - assembled.tail_count)
        narrative = render_branch(assembled.messages, own_exchange_id, live_ids)
        if narrative:
            narrative[-1] = with_date_envelope(narrative[-1])
        return narrative, assembled.tail_count

    async def live_exchange_ids(self, own_exchange_id: str | None = None) -> frozenset[str]:
        return frozenset(
            item.id
            for item in await self._runner._stores.exchanges.list_live(self._runner.dialog_id)
            if item.id != own_exchange_id
        )

    def trim(self, drop: int) -> None:
        if drop <= 0:
            return
        runtime = self._runner._runtime
        del runtime.narrative[:drop]
        for process in runtime.processes.values():
            process.watermark = max(0, process.watermark - drop)

    async def sync_branch(self, process: Process, *, force: bool = False) -> None:
        runtime = self._runner._runtime
        if not process.narrative_built:
            return
        if not force and len(runtime.narrative) == process.watermark:
            return
        narrative, watermark = await self.assemble(process.exchange_id)
        private = process.branch[process.synced_len :]
        process.branch[:] = [self.system_message(), *narrative, *private]
        process.synced_len = 1 + len(narrative)
        process.watermark = watermark

    async def compact_after_run_final(self) -> None:
        try:
            await self.assemble()
        except Exception:
            logger.exception("post-run compaction check failed: dialog=%s", self._runner.dialog_id)

    async def persist(
        self,
        message: ChatMessage,
        usage: Usage | None = None,
        client_message_id: str | None = None,
    ) -> str:
        append = MessageAppend(self._runner.dialog_id, message, usage, client_message_id)
        return await self._runner._stores.messages.append(append)
