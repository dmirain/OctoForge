"""Tool that searches the web through an injected SearchProvider."""

from typing import Any

from octoforge_core.search.api import SearchError, SearchProvider, SearchResponse
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec

DEFAULT_NUM_RESULTS = 5
MAX_NUM_RESULTS = 10
MAX_OUTPUT_CHARS = 4000
TRUNCATED_SUFFIX = "\n...[truncated]"
NO_RESULTS_MESSAGE = "no results"

TOOL_NAME = "web_search"
TOOL_DESCRIPTION = (
    "Search the public web; returns titles, links and snippets. For public facts and "
    "current events only. Anything specific to this user or this installation — how a "
    "task is done here, an API contract, the user's own data or past decisions — is "
    "not on the web: look it up with recall instead."
)
PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query text"},
        "num_results": {
            "type": "integer",
            "description": f"Results to return, 1-{MAX_NUM_RESULTS}; default {DEFAULT_NUM_RESULTS}",
        },
    },
    "required": ["query"],
}


class WebSearchTool:
    """Runs a web search through the provider port and formats the top results."""

    def __init__(self, provider: SearchProvider) -> None:
        self._provider = provider

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL_NAME,
            description=TOOL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    def visible_to(self, context: ToolContext) -> bool:
        """Hide the tool when the caller's plan does not include web search."""
        return feature_enabled(context.enabled_features, FeatureCode.WEB_SEARCH)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):  # defence in depth behind the spec filter
            return feature_refusal(FeatureCode.WEB_SEARCH)
        query = str(arguments["query"])
        num_results = _num_results(arguments.get("num_results"))
        try:
            response = await self._provider.search(query, num_results)
        except SearchError as exc:
            return f"error: {exc}"
        return _format_results(response)


def _num_results(raw: object) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return min(max(raw, 1), MAX_NUM_RESULTS)
    return DEFAULT_NUM_RESULTS


def _format_results(response: SearchResponse) -> str:
    sections: list[str] = []
    if response.answer is not None:
        sections.append(f"Answer box: {response.answer}")
    for position, result in enumerate(response.results, start=1):
        lines = [f"{position}. {result.title}", result.link]
        if result.snippet.strip():
            lines.append(result.snippet)
        sections.append("\n".join(lines))
    if not sections:
        return NO_RESULTS_MESSAGE
    return _truncate("\n\n".join(sections))


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + TRUNCATED_SUFFIX
