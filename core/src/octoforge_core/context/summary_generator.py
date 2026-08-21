"""LLM generation and metering of one rolling dialog summary."""

import uuid

from octoforge_core.context.prompts import (
    SUMMARY_SYSTEM_PROMPT,
    format_merge_request,
    parse_summary_reply,
)
from octoforge_core.context.types import ArchivedMessage, DialogueSummary
from octoforge_core.domain import ChatMessage, Dialog, MessageRole
from octoforge_core.ports import LLMClient
from octoforge_core.tariffs.api import UsageEvent, UsageKind, UsageOrigin, UsageRecorder
from octoforge_core.time import utc_now


class SummaryGenerator:
    """Merge previous summaries and one archive segment through the LLM."""

    def __init__(self, llm: LLMClient, meter: UsageRecorder | None) -> None:
        self._llm = llm
        self._meter = meter

    async def generate(
        self,
        dialog: Dialog,
        segment: list[ArchivedMessage],
        previous: list[DialogueSummary],
    ) -> DialogueSummary:
        completion = await self._llm.complete(
            [
                ChatMessage(role=MessageRole.SYSTEM, content=SUMMARY_SYSTEM_PROMPT),
                ChatMessage(role=MessageRole.USER, content=format_merge_request(previous, segment)),
            ]
        )
        if self._meter is not None and completion.usage is not None:
            await self._meter.record(
                UsageEvent(
                    user_id=dialog.user_id,
                    kind=UsageKind.LLM_COMPACTION,
                    origin=UsageOrigin.INTERACTIVE,
                    prompt_tokens=completion.usage.prompt_tokens,
                    completion_tokens=completion.usage.completion_tokens,
                    dialog_id=dialog.id,
                )
            )
        topics, content = parse_summary_reply(completion.message.content)
        seq_from = min([segment[0].seq, *(summary.seq_from for summary in previous)])
        return DialogueSummary(
            id=uuid.uuid4().hex,
            dialog_id=dialog.id,
            seq_from=seq_from,
            seq_to=segment[-1].seq,
            topics=topics,
            content=content,
            created_at=utc_now(),
        )
