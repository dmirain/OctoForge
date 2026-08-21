"""Pure text and branch transformations used by dialog actors."""

from collections.abc import Sequence
from dataclasses import replace

from octoforge_core.agent.events import (
    Failed,
    Finished,
    LoopEvent,
    ProcessStarted,
    ReasoningDelta,
    TextDelta,
)
from octoforge_core.domain import AttachmentKind, ChatMessage, MessageRole
from octoforge_core.tasks.api import Task, TaskStatus
from octoforge_core.time import utc_now

from .runner_constants import (
    CURRENT_DATE_FORMAT,
    DATE_ENVELOPE_TEMPLATE,
    MATERIAL_DIGEST_ELLIPSIS,
    MATERIAL_TITLE_ANONYMOUS,
    MATERIAL_TITLE_IMAGES,
)
from .runner_process import task_client_source


def latest_assistant_with_content(branch: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(branch):
        if message.role is MessageRole.TOOL:
            continue
        if message.role is MessageRole.ASSISTANT and message.content:
            return message
        return None
    return None


def with_date_envelope(message: ChatMessage) -> ChatMessage:
    return ChatMessage(
        role=message.role,
        content=DATE_ENVELOPE_TEMPLATE.format(
            now=utc_now().strftime(CURRENT_DATE_FORMAT), content=message.content
        ),
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
    )


def untitled(message: ChatMessage) -> str:
    if any(item.kind is AttachmentKind.IMAGE for item in message.attachments):
        return MATERIAL_TITLE_IMAGES
    return MATERIAL_TITLE_ANONYMOUS


def bounded_preview(pieces: Sequence[str], budget: int) -> str:
    if not pieces:
        return ""
    share = max(budget // len(pieces), len(MATERIAL_DIGEST_ELLIPSIS) + 2)
    return "\n".join(middle_out(piece, share) for piece in pieces)


def middle_out(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = limit - len(MATERIAL_DIGEST_ELLIPSIS)
    head = keep // 2
    return text[:head] + MATERIAL_DIGEST_ELLIPSIS + text[len(text) - (keep - head) :]


def muted_after_ask(event: LoopEvent) -> LoopEvent | None:
    if isinstance(event, (TextDelta, ReasoningDelta)):
        return None
    if isinstance(event, Finished):
        return replace(event, message=replace(event.message, content=""))
    return event


def silent_done(task: Task) -> bool:
    return task.status is TaskStatus.DONE and not (task.result or "").strip()


def delivery_started(task: Task) -> ProcessStarted:
    return ProcessStarted(
        process_id=task.id,
        title=task.title,
        source_client_message_id=task_client_source(task),
    )


def stored_terminal(task: Task, default_error: str) -> Finished | Failed | None:
    if task.status is TaskStatus.DONE:
        message = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=task.result or "",
            task_id=task.id,
        )
        return Finished(message=message, source_client_message_id=task_client_source(task))
    if task.status is TaskStatus.FAILED:
        return Failed(error=task.error or default_error)
    return None
