"""Date parsing and result formatting for history_search."""

from datetime import UTC, datetime, timedelta

from octoforge_core.context.api import ArchivedMessage
from octoforge_core.tools.errors import ToolArgumentsError

ENTRY_TEMPLATE = "[{created}] seq {seq} {role}: {snippet}"
DATE_FORMAT = "%Y-%m-%d %H:%M"
DATE_ONLY_LENGTH = 10
SNIPPET_CHARS = 300


def parse_date(raw: object, name: str, *, end: bool) -> datetime | None:
    """Parse an ISO date or datetime into an aware UTC boundary."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError(f"{name} must be an ISO date or datetime string")
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolArgumentsError(f"{name} must be an ISO date or datetime string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if end and len(text) == DATE_ONLY_LENGTH:
        parsed += timedelta(days=1)
    return parsed


def format_entry(message: ArchivedMessage) -> str:
    return ENTRY_TEMPLATE.format(
        created=message.created_at.strftime(DATE_FORMAT),
        seq=message.seq,
        role=message.role.value,
        snippet=_snippet(message.content),
    )


def _snippet(content: str) -> str:
    return " ".join(content.split())[:SNIPPET_CHARS]
