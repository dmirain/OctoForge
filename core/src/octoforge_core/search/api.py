"""Public boundary of the web-search module.

Everything the rest of the system (tools) may know about web search lives
here: the `SearchProvider` port, the transport-neutral DTOs and the module
error. Providers return plain result lists, so neither the tool nor an
installer-written provider deals with a vendor-specific payload.

The default implementation is `SerperSearchProvider` (serper.dev Google
API); an installer registers `WebSearchTool` with its own provider (Bing,
Brave, Tavily, ...) in its composition root without touching the core.
"""

from dataclasses import dataclass
from typing import Protocol


class SearchError(Exception):
    """Raised when the search backend fails (network, auth, quota, bad status)."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One organic search hit."""

    title: str
    link: str
    snippet: str


@dataclass(frozen=True, slots=True)
class SearchResponse:
    """The outcome of one search: organic hits plus an optional direct answer."""

    results: tuple[SearchResult, ...]
    answer: str | None = None


class SearchProvider(Protocol):
    """Port the web_search tool searches through."""

    async def search(self, query: str, num_results: int) -> SearchResponse:
        """Return up to `num_results` hits for the query; raise `SearchError` on failure."""
        ...
