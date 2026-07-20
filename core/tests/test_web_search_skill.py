"""Tests for the web_search skill over a fake SearchProvider (port substitution)."""

from octoforge_core.search.api import SearchError, SearchResponse, SearchResult
from octoforge_core.skills.base import SkillContext
from octoforge_core.skills.basic.web_search import WebSearchSkill

CONTEXT = SkillContext(user_id="alice", channel="telegram", dialog_id="dialog-1")
MAX_OUTPUT_WITH_SUFFIX = 4000 + 20
QUERY = "meaning of life"
NUM_DEFAULT = 5
NUM_MIN = 1
NUM_MAX = 10

FIRST = SearchResult(title="First", link="https://first.example", snippet="alpha")
SECOND = SearchResult(title="Second", link="https://second.example", snippet="")
ANSWER = "42"


class FakeSearchProvider:
    """SearchProvider stub: scripted response or error, records the calls."""

    def __init__(
        self,
        response: SearchResponse | None = None,
        error: SearchError | None = None,
    ) -> None:
        self._response = response if response is not None else SearchResponse(results=())
        self._error = error
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, num_results: int) -> SearchResponse:
        self.calls.append((query, num_results))
        if self._error is not None:
            raise self._error
        return self._response


def make_skill(provider: FakeSearchProvider) -> WebSearchSkill:
    return WebSearchSkill(provider=provider)


async def test_results_are_formatted_with_positions_and_snippets() -> None:
    provider = FakeSearchProvider(response=SearchResponse(results=(FIRST, SECOND), answer=ANSWER))

    result = await make_skill(provider).execute({"query": QUERY}, CONTEXT)

    assert result.startswith(f"Answer box: {ANSWER}")
    assert "1. First\nhttps://first.example\nalpha" in result
    assert "2. Second\nhttps://second.example" in result
    assert provider.calls == [(QUERY, NUM_DEFAULT)]


async def test_num_results_is_clamped_before_reaching_the_provider() -> None:
    provider = FakeSearchProvider()
    skill = make_skill(provider)

    await skill.execute({"query": "q", "num_results": 0}, CONTEXT)
    await skill.execute({"query": "q", "num_results": 99}, CONTEXT)
    await skill.execute({"query": "q", "num_results": True}, CONTEXT)
    await skill.execute({"query": "q", "num_results": 3}, CONTEXT)

    assert [num for _, num in provider.calls] == [NUM_MIN, NUM_MAX, NUM_DEFAULT, 3]


async def test_empty_answer_is_reported() -> None:
    result = await make_skill(FakeSearchProvider()).execute({"query": "q"}, CONTEXT)

    assert result == "no results"


async def test_search_error_is_an_error_string() -> None:
    provider = FakeSearchProvider(error=SearchError("search API returned HTTP 403"))

    result = await make_skill(provider).execute({"query": "q"}, CONTEXT)

    assert result == "error: search API returned HTTP 403"


async def test_long_output_is_truncated() -> None:
    provider = FakeSearchProvider(
        response=SearchResponse(
            results=tuple(
                SearchResult(title=f"T{i}", link=f"https://e{i}.example", snippet="s" * 900)
                for i in range(NUM_MAX)
            )
        )
    )

    result = await make_skill(provider).execute({"query": "q"}, CONTEXT)

    assert len(result) <= MAX_OUTPUT_WITH_SUFFIX
    assert result.endswith("\n...[truncated]")
