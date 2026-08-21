"""Persistence of a useful partial turn from a cancelled process."""

from dataclasses import replace
from typing import TYPE_CHECKING

from octoforge_core.context.api import INTERRUPTED_NOTE
from octoforge_core.domain import ChatMessage, MessageRole

from .runner_process import Process
from .runner_text import latest_assistant_with_content

if TYPE_CHECKING:
    from .runner import ConversationRunner


async def salvage(
    runner: "ConversationRunner", process: Process
) -> tuple[ChatMessage, ChatMessage] | None:
    last = latest_assistant_with_content(process.branch[process.synced_len :])
    if last is None:
        return None
    salvaged = replace(last, task_id=process.task_id)
    note = ChatMessage(role=MessageRole.SYSTEM, content=INTERRUPTED_NOTE)
    await runner._stores.messages.append_pair(runner.dialog_id, salvaged, note)
    return salvaged, note
