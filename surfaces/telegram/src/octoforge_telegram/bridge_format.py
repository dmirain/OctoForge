"""Pure formatting of Telegram answer drafts and status events."""

from octoforge_core.agent.events import LoopEvent, RetryScheduled, ToolCallFailed

from octoforge_telegram.bridge_state import (
    MAX_PLAIN_DOTS,
    RETRY_LINE_TEMPLATE,
    STATUS_SEPARATOR,
    TOOL_FAIL_LINE_TEMPLATE,
    TOOL_GROUPS,
    TOOL_LINE_TEMPLATE,
    Draft,
    StatusEntry,
)


def split_markdown(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        rest = line
        while len(rest) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(rest[:limit])
            rest = rest[limit:]
        if len(current) + len(rest) > limit:
            chunks.append(current)
            current = ""
        current += rest
    if current:
        chunks.append(current)
    trimmed = [stripped for chunk in chunks if (stripped := chunk.strip("\n"))]
    return trimmed or [text[:limit]]


def reply_target(source_client_message_id: str | None) -> int | None:
    if source_client_message_id is None or not source_client_message_id.isdigit():
        return None
    return int(source_client_message_id)


def notice_line(event: LoopEvent) -> str | None:
    if isinstance(event, ToolCallFailed):
        return TOOL_FAIL_LINE_TEMPLATE.format(name=event.call.name, error=event.error)
    if isinstance(event, RetryScheduled):
        return RETRY_LINE_TEMPLATE.format(
            reason=event.reason,
            attempt=event.attempt,
            delay=event.delay_seconds,
        )
    return None


def tool_group(name: str) -> str:
    return TOOL_GROUPS.get(name) or TOOL_LINE_TEMPLATE.format(name=name)


def count_status(draft: Draft, label: str) -> None:
    head = draft.status[0] if draft.status else None
    if head is not None and head.counted and head.label == label:
        head.count += 1
        return
    draft.status.insert(0, StatusEntry(label))


def compose(draft: Draft) -> str:
    entries = [
        f"{entry.label} {_progress(entry.count)}" if entry.counted else entry.label
        for entry in draft.status
    ]
    parts: list[str] = []
    if entries:
        parts.append("```\n" + STATUS_SEPARATOR.join(entries) + "\n```")
    text = draft.buffer.rstrip("\n")
    if text:
        parts.append(text)
    return "\n".join(parts)


def _progress(count: int) -> str:
    return "·" * count if count <= MAX_PLAIN_DOTS else f"··{count}"
