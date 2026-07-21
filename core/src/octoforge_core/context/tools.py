"""Tool searching the full message archive of the current dialog."""

from datetime import UTC, datetime, timedelta
from typing import Any

from octoforge_core.context.api import ArchivedMessage, ArchiveFilter, MessageArchive, SummaryStore
from octoforge_core.skills.base import SkillContext, SkillSpec
from octoforge_core.skills.errors import SkillArgumentsError

SKILL_NAME = "history_search"
SKILL_DESCRIPTION = (
    "Search the full history of this conversation (including what fell out of the "
    "recent verbatim tail) by a case-insensitive substring over message content. "
    "Optionally restrict the hits to a topic tag of the compressed summaries or to "
    "a date range. Use it when the user refers to something discussed earlier that "
    "is not covered by the topic summaries or the recent tail."
)
NO_HITS_MESSAGE = "no matching messages"
ENTRY_TEMPLATE = "[{created}] seq {seq} {role}: {snippet}"
DATE_FORMAT = "%Y-%m-%d %H:%M"
DATE_ONLY_LENGTH = 10
SNIPPET_CHARS = 300
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Substring to look for in message content"},
        "topic": {
            "type": "string",
            "description": "Restrict hits to the seq ranges of summaries tagged with this topic",
        },
        "date_from": {
            "type": "string",
            "description": "ISO date/datetime, inclusive lower bound (UTC when naive)",
        },
        "date_to": {
            "type": "string",
            "description": "ISO date/datetime, inclusive upper bound (UTC when naive)",
        },
        "limit": {"type": "integer", "description": "How many messages to return"},
    },
    "required": ["query"],
}


class HistorySearchSkill:
    """Thin adapter over the MessageArchive and SummaryStore ports."""

    def __init__(
        self,
        archive: MessageArchive,
        summaries: SummaryStore,
        default_limit: int,
        max_limit: int,
    ) -> None:
        self._archive = archive
        self._summaries = summaries
        self._default_limit = default_limit
        self._max_limit = max_limit

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        """Validate arguments, search the dialog's archive and format the hits."""
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise SkillArgumentsError("query must be a non-empty string")
        limit = self._limit(arguments.get("limit"))
        seq_ranges = await self._seq_ranges(context.dialog_id, arguments.get("topic"))
        if seq_ranges == ():
            return NO_HITS_MESSAGE  # a topic filter that matched no summary
        hits = await self._archive.search(
            context.dialog_id,
            query,
            filters=ArchiveFilter(
                seq_ranges=seq_ranges,
                date_from=_parse_date(arguments.get("date_from"), "date_from", end=False),
                date_to=_parse_date(arguments.get("date_to"), "date_to", end=True),
            ),
            limit=limit,
        )
        if not hits:
            return NO_HITS_MESSAGE
        return "\n".join(
            f"{index}. {_format_entry(hit)}" for index, hit in enumerate(hits, start=1)
        )

    def _limit(self, raw: object) -> int:
        if raw is None:
            return self._default_limit
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise SkillArgumentsError("limit must be an integer")
        if raw < 1 or raw > self._max_limit:
            raise SkillArgumentsError(f"limit must be between 1 and {self._max_limit}")
        return raw

    async def _seq_ranges(
        self, dialog_id: str, raw_topic: object
    ) -> tuple[tuple[int, int], ...] | None:
        """Map a topic filter to summary seq ranges; None when no filter was given."""
        if raw_topic is None:
            return None
        if not isinstance(raw_topic, str) or not raw_topic.strip():
            raise SkillArgumentsError("topic must be a non-empty string")
        summaries = await self._summaries.find_by_topic(dialog_id, raw_topic)
        return tuple((summary.seq_from, summary.seq_to) for summary in summaries)


def _parse_date(raw: object, name: str, *, end: bool) -> datetime | None:
    """Parse an ISO date/datetime into aware UTC.

    Naive values are read as UTC (the project is UTC-only). A date-only upper
    bound is inclusive: it is moved to the next midnight (the store's date_to
    is exclusive).
    """
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise SkillArgumentsError(f"{name} must be an ISO date or datetime string")
    text = raw.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SkillArgumentsError(f"{name} must be an ISO date or datetime string") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if end and len(text) == DATE_ONLY_LENGTH:
        parsed += timedelta(days=1)
    return parsed


def _format_entry(message: ArchivedMessage) -> str:
    return ENTRY_TEMPLATE.format(
        created=message.created_at.strftime(DATE_FORMAT),
        seq=message.seq,
        role=message.role.value,
        snippet=_snippet(message.content),
    )


def _snippet(content: str) -> str:
    one_line = " ".join(content.split())
    return one_line[:SNIPPET_CHARS]
