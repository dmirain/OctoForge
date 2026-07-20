"""SerperSearchProvider: Google search through the serper.dev API."""

from typing import Any

import httpx

from octoforge_core.search.api import SearchError, SearchResponse, SearchResult

SERPER_SEARCH_URL = "https://google.serper.dev/search"
API_KEY_HEADER = "X-API-KEY"


class SerperSearchProvider:
    """SearchProvider over the serper.dev Google search endpoint."""

    def __init__(self, http_client: httpx.AsyncClient, api_key: str) -> None:
        self._http = http_client
        self._api_key = api_key

    async def search(self, query: str, num_results: int) -> SearchResponse:
        """POST the query to serper.dev and map the payload to the neutral DTOs."""
        try:
            response = await self._http.post(
                SERPER_SEARCH_URL,
                headers={API_KEY_HEADER: self._api_key},
                json={"q": query, "num": num_results},
            )
        except httpx.HTTPError as exc:
            raise SearchError(f"search failed: {type(exc).__name__}") from exc
        if response.status_code != httpx.codes.OK:
            raise SearchError(f"search API returned HTTP {response.status_code}")
        return _parse_response(response.json(), num_results)


def _parse_response(payload: dict[str, Any], num_results: int) -> SearchResponse:
    results: list[SearchResult] = []
    organic = payload.get("organic")
    if isinstance(organic, list):
        for item in organic[:num_results]:
            result = _parse_organic(item)
            if result is not None:
                results.append(result)
    return SearchResponse(results=tuple(results), answer=_answer_box(payload.get("answerBox")))


def _answer_box(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("answer", "snippet"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _parse_organic(raw: object) -> SearchResult | None:
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    link = raw.get("link")
    if not isinstance(title, str) or not isinstance(link, str):
        return None
    snippet = raw.get("snippet")
    return SearchResult(
        title=title,
        link=link,
        snippet=snippet if isinstance(snippet, str) else "",
    )
