"""Tests for the Rich Message construct detector."""

import pytest

from octoforge_web.telegram.rich import needs_rich_message

TABLE = "| col | val |\n| --- | --- |\n| a | 1 |"
TASK_LIST = "- [ ] buy milk\n- [x] write tests"
DETAILS = "<details><summary>more</summary>hidden</details>"
MATH_BLOCK = "answer:\n$$x^2 + y^2 = z^2$$"


@pytest.mark.parametrize("text", [TABLE, TASK_LIST, DETAILS, MATH_BLOCK])
def test_constructs_degraded_by_html_trigger_rich(text: str) -> None:
    assert needs_rich_message(text)


@pytest.mark.parametrize(
    "text",
    [
        "plain prose, **bold** and `code`",
        "- bullet one\n- bullet two",
        "a single | pipe in a sentence",
        "| header | only |\n| no separator row |",
        "inline $x$ math stays legacy",
        "price: $$ 5",
    ],
)
def test_plain_markdown_stays_on_the_legacy_path(text: str) -> None:
    assert not needs_rich_message(text)
