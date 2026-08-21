"""Pure hot-tail sizing, segment selection and topics rendering."""

from dataclasses import dataclass

from octoforge_core.context.types import INTERRUPTED_NOTE, ArchivedMessage, DialogueSummary
from octoforge_core.domain import ChatMessage, MessageRole

DEFAULT_HOT_MAX_CHARS = 12000
DEFAULT_COMPACT_TARGET_CHARS = 6000
DEFAULT_MODEL_CONTEXT_TOKENS = 0
DEFAULT_CONTEXT_BUFFER_TOKENS = 2000
SEGMENT_FETCH_LIMIT = 500
TOPICS_BLOCK_HEADER = (
    "Compressed summaries of earlier topics of this conversation "
    "(the verbatim recent history follows):"
)
TOPIC_ENTRY_TEMPLATE = "[seq {seq_from}-{seq_to}] (topics: {topics}) {content}"
NO_TOPICS = "-"


@dataclass(frozen=True, slots=True)
class CompactorConfig:
    hot_max_chars: int = DEFAULT_HOT_MAX_CHARS
    compact_target_chars: int = DEFAULT_COMPACT_TARGET_CHARS
    model_context_tokens: int = DEFAULT_MODEL_CONTEXT_TOKENS
    context_buffer_tokens: int = DEFAULT_CONTEXT_BUFFER_TOKENS


def select_compact_segment(
    tail: list[ArchivedMessage],
    target_chars: int,
) -> list[ArchivedMessage]:
    """Pick an oldest whole-message prefix without swallowing the newest message."""
    segment: list[ArchivedMessage] = []
    total = 0
    for message in tail[:-1]:
        if segment and total + len(message.content) > target_chars:
            break
        segment.append(message)
        total += len(message.content)
    if segment and len(tail) > len(segment) and _is_split_pair(segment[-1], tail[len(segment)]):
        if len(segment) + 1 == len(tail):
            segment.pop()
        else:
            segment.append(tail[len(segment)])
    return segment


def tail_of(history: list[ChatMessage], tail_count: int) -> list[ChatMessage]:
    count = min(tail_count, len(history))
    return [] if count == 0 else history[-count:]


def content_chars(messages: list[ChatMessage]) -> int:
    return sum(len(message.content) for message in messages)


def topics_block(summaries: list[DialogueSummary]) -> ChatMessage:
    lines = [TOPICS_BLOCK_HEADER]
    for summary in summaries:
        lines.append(
            TOPIC_ENTRY_TEMPLATE.format(
                seq_from=summary.seq_from,
                seq_to=summary.seq_to,
                topics=", ".join(summary.topics) if summary.topics else NO_TOPICS,
                content=summary.content,
            )
        )
    return ChatMessage(role=MessageRole.SYSTEM, content="\n".join(lines))


def _is_split_pair(last_in: ArchivedMessage, first_out: ArchivedMessage) -> bool:
    return (
        last_in.role is MessageRole.ASSISTANT
        and first_out.role is MessageRole.SYSTEM
        and first_out.content == INTERRUPTED_NOTE
    )
