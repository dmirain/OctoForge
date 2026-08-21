"""Tool searching the full message archive of the current dialog."""

from dataclasses import dataclass
from typing import Any

from octoforge_core.context.api import ArchiveFilter, ArchiveSearch, MessageArchive, SummaryStore
from octoforge_core.context.history_search_format import format_entry, parse_date
from octoforge_core.context.history_search_schema import (
    PARAMETERS_SCHEMA,
    TOOL_DESCRIPTION,
    TOOL_NAME,
)
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

NO_HITS_MESSAGE = "no matching messages"


@dataclass(frozen=True, slots=True)
class HistorySearchLimits:
    default: int
    maximum: int


class HistorySearchTool:
    """Thin adapter over the MessageArchive and SummaryStore ports."""

    def __init__(
        self,
        archive: MessageArchive,
        summaries: SummaryStore,
        limits: HistorySearchLimits,
    ) -> None:
        self._archive = archive
        self._summaries = summaries
        self._limits = limits

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        """Validate arguments, search the dialog's archive and format the hits."""
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolArgumentsError("query must be a non-empty string")
        limit = self._limit(arguments.get("limit"))
        seq_ranges = await self._seq_ranges(context.dialog_id, arguments.get("topic"))
        if seq_ranges == ():
            return NO_HITS_MESSAGE  # a topic filter that matched no summary
        hits = await self._archive.search(
            ArchiveSearch(
                context.dialog_id,
                query,
                ArchiveFilter(
                    seq_ranges=seq_ranges,
                    date_from=parse_date(arguments.get("date_from"), "date_from", end=False),
                    date_to=parse_date(arguments.get("date_to"), "date_to", end=True),
                ),
                limit,
            )
        )
        if not hits:
            return NO_HITS_MESSAGE
        return "\n".join(f"{index}. {format_entry(hit)}" for index, hit in enumerate(hits, start=1))

    def _limit(self, raw: object) -> int:
        if raw is None:
            return self._limits.default
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ToolArgumentsError("limit must be an integer")
        if raw < 1 or raw > self._limits.maximum:
            raise ToolArgumentsError(f"limit must be between 1 and {self._limits.maximum}")
        return raw

    async def _seq_ranges(
        self, dialog_id: str, raw_topic: object
    ) -> tuple[tuple[int, int], ...] | None:
        """Map a topic filter to summary seq ranges; None when no filter was given."""
        if raw_topic is None:
            return None
        if not isinstance(raw_topic, str) or not raw_topic.strip():
            raise ToolArgumentsError("topic must be a non-empty string")
        summaries = await self._summaries.find_by_topic(dialog_id, raw_topic)
        return tuple((summary.seq_from, summary.seq_to) for summary in summaries)
