"""Summarization prompt of the context module and the reply parsing helpers."""

from octoforge_core.context.api import ArchivedMessage, DialogueSummary

TOPICS_PREFIX = "TOPICS:"
SUMMARY_PREFIX = "SUMMARY:"
MAX_TOPICS = 4
SEGMENT_LINE_TEMPLATE = "[{seq}] {role}: {content}"
NO_PREVIOUS_SUMMARY = "Previous summary: none."

SUMMARY_SYSTEM_PROMPT = (
    "You maintain a durable summary of a conversation.\n"
    "You receive the previous summary (possibly none) and a new conversation segment. "
    "Produce the UPDATED summary: preserve what is still true, drop what is stale or "
    "superseded, merge in the new facts.\n"
    "Reply in exactly this format:\n"
    f"{TOPICS_PREFIX} tag1, tag2\n"
    f"{SUMMARY_PREFIX}\n"
    "<updated summary>\n"
    "Summary structure:\n"
    "- Goal: what the user is trying to achieve.\n"
    "- State: current state, what has been done.\n"
    "- Next: open tasks and next steps.\n"
    "- Decisions: key decisions, facts and agreements.\n"
    "Rules:\n"
    "1. Emit 1-4 topic tags: short, lowercase, normalized (singular, no punctuation).\n"
    "2. Keep exact identifiers: paths, commands, error texts, names, numbers, dates.\n"
    "3. Drop small talk and phrasing; keep the summary compact.\n"
    "4. Write in the language of the conversation."
)


def format_segment(segment: list[ArchivedMessage]) -> str:
    """Render the archive segment handed to the summarization call."""
    return "\n".join(
        SEGMENT_LINE_TEMPLATE.format(
            seq=message.seq, role=message.role.value, content=message.content
        )
        for message in segment
    )


def format_merge_request(previous: list[DialogueSummary], segment: list[ArchivedMessage]) -> str:
    """Render the rolling-merge request: previous summary + the new segment."""
    parts: list[str] = []
    if previous:
        parts.append("Previous summary:")
        parts.extend(f"- {summary.content}" for summary in previous)
    else:
        parts.append(NO_PREVIOUS_SUMMARY)
    parts.append("")
    parts.append("New segment:")
    parts.append(format_segment(segment))
    return "\n".join(parts)


def parse_summary_reply(text: str) -> tuple[tuple[str, ...], str]:
    """Split the LLM reply into (topics, content); tolerant of format slips.

    A missing TOPICS line yields no tags; a missing SUMMARY marker treats the
    whole reply (minus the topics line) as the content.
    """
    topics: tuple[str, ...] = ()
    kept: list[str] = []
    in_summary = False
    for line in text.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if not in_summary and upper.startswith(TOPICS_PREFIX):
            topics = _parse_topics(stripped[len(TOPICS_PREFIX) :])
        elif upper.startswith(SUMMARY_PREFIX):
            in_summary = True
            remainder = stripped[len(SUMMARY_PREFIX) :].strip()
            if remainder:
                kept.append(remainder)
        elif in_summary:
            kept.append(line)
    if not in_summary:
        kept = [
            line for line in text.splitlines() if not line.strip().upper().startswith(TOPICS_PREFIX)
        ]
    content = "\n".join(kept).strip()
    return topics, content if content else text.strip()


def _parse_topics(raw: str) -> tuple[str, ...]:
    tags: list[str] = []
    for item in raw.split(","):
        tag = item.strip().lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tuple(tags[:MAX_TOPICS])
