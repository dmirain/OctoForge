"""Basic skill that searches the web through the serper.dev Google API."""

from typing import Any

import httpx

from octoforge_core.skills.base import SkillContext, SkillSpec

SERPER_SEARCH_URL = "https://google.serper.dev/search"
API_KEY_HEADER = "X-API-KEY"
DEFAULT_NUM_RESULTS = 5
MAX_NUM_RESULTS = 10
MAX_OUTPUT_CHARS = 4000
TRUNCATED_SUFFIX = "\n...[truncated]"
NO_RESULTS_MESSAGE = "no results"

SKILL_NAME = "web_search"
SKILL_DESCRIPTION = "Search the web (Google via serper.dev); returns titles, links and snippets."
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


class WebSearchSkill:
    """Runs a Google search via serper.dev and formats the top results."""

    def __init__(self, http_client: httpx.AsyncClient, api_key: str) -> None:
        self._http = http_client
        self._api_key = api_key

    @property
    def spec(self) -> SkillSpec:
        return SkillSpec(
            name=SKILL_NAME,
            description=SKILL_DESCRIPTION,
            parameters_schema=PARAMETERS_SCHEMA,
        )

    async def execute(self, arguments: dict[str, Any], context: SkillContext) -> str:
        query = str(arguments["query"])
        num_results = _num_results(arguments.get("num_results"))
        try:
            response = await self._http.post(
                SERPER_SEARCH_URL,
                headers={API_KEY_HEADER: self._api_key},
                json={"q": query, "num": num_results},
            )
        except httpx.HTTPError as exc:
            return f"error: search failed: {type(exc).__name__}"
        if response.status_code != httpx.codes.OK:
            return f"error: search API returned HTTP {response.status_code}"
        return _format_results(response.json())


def _num_results(raw: object) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return min(max(raw, 1), MAX_NUM_RESULTS)
    return DEFAULT_NUM_RESULTS


def _format_results(payload: dict[str, Any]) -> str:
    sections: list[str] = []
    answer = _answer_box(payload.get("answerBox"))
    if answer is not None:
        sections.append(f"Answer box: {answer}")
    organic = payload.get("organic")
    if isinstance(organic, list):
        for position, item in enumerate(organic[:MAX_NUM_RESULTS], start=1):
            entry = _format_organic(position, item)
            if entry is not None:
                sections.append(entry)
    if not sections:
        return NO_RESULTS_MESSAGE
    return _truncate("\n\n".join(sections))


def _answer_box(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("answer", "snippet"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _format_organic(position: int, raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    link = raw.get("link")
    if not isinstance(title, str) or not isinstance(link, str):
        return None
    snippet = raw.get("snippet")
    lines = [f"{position}. {title}", link]
    if isinstance(snippet, str) and snippet.strip():
        lines.append(snippet)
    return "\n".join(lines)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + TRUNCATED_SUFFIX
