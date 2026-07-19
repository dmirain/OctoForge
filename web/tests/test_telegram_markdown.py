"""Tests for Markdown-to-Telegram-HTML conversion and tag-safe splitting."""

import re

from octoforge_web.telegram.markdown import markdown_to_telegram_html, split_html_safe

SPLIT_LIMIT = 50
TAG_RE = re.compile(r"<[^>]*>")


def strip_tags(html: str) -> str:
    return TAG_RE.sub("", html)


def assert_balanced(chunk: str) -> None:
    for tag in ("b", "i", "s", "code", "pre", "a", "blockquote"):
        assert chunk.count(f"<{tag}>") + chunk.count(f"<{tag} ") == chunk.count(f"</{tag}>")


def test_plain_text_passes_through() -> None:
    assert markdown_to_telegram_html("hello world") == "hello world"


def test_special_chars_are_escaped() -> None:
    assert markdown_to_telegram_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_raw_html_is_escaped_not_interpreted() -> None:
    assert markdown_to_telegram_html("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"


def test_bold_star_and_underscore() -> None:
    assert markdown_to_telegram_html("**bold**") == "<b>bold</b>"
    assert markdown_to_telegram_html("__bold__") == "<b>bold</b>"


def test_italic_star_and_underscore() -> None:
    assert markdown_to_telegram_html("*it*") == "<i>it</i>"
    assert markdown_to_telegram_html("_it_") == "<i>it</i>"


def test_underscores_inside_words_are_not_italic() -> None:
    assert markdown_to_telegram_html("snake_case_name") == "snake_case_name"


def test_strikethrough() -> None:
    assert markdown_to_telegram_html("~~gone~~") == "<s>gone</s>"


def test_inline_code() -> None:
    assert markdown_to_telegram_html("run `make check` now") == "run <code>make check</code> now"


def test_inline_code_content_is_escaped() -> None:
    assert markdown_to_telegram_html("`a < b`") == "<code>a &lt; b</code>"


def test_fenced_block_with_language() -> None:
    text = "```python\nprint(1)\n```"
    assert markdown_to_telegram_html(text) == "<pre>print(1)</pre>"


def test_fenced_block_content_is_escaped() -> None:
    text = "```\na < b\n```"
    assert markdown_to_telegram_html(text) == "<pre>a &lt; b</pre>"


def test_unclosed_fence_runs_to_the_end() -> None:
    text = "before\n```\ncode line"
    assert markdown_to_telegram_html(text) == "before\n<pre>code line</pre>"


def test_link() -> None:
    text = "[docs](https://example.com)"
    assert markdown_to_telegram_html(text) == '<a href="https://example.com">docs</a>'


def test_link_url_is_attribute_escaped() -> None:
    text = "[docs](https://example.com?a=1&b=2)"
    assert markdown_to_telegram_html(text) == '<a href="https://example.com?a=1&amp;b=2">docs</a>'


def test_link_with_unsupported_scheme_stays_literal() -> None:
    text = "[x](javascript:alert)"
    assert markdown_to_telegram_html(text) == "[x](javascript:alert)"


def test_bold_inside_link_label() -> None:
    text = "[**docs**](https://example.com)"
    assert markdown_to_telegram_html(text) == '<a href="https://example.com"><b>docs</b></a>'


def test_link_inside_bold() -> None:
    text = "**[docs](https://example.com)**"
    assert markdown_to_telegram_html(text) == '<b><a href="https://example.com">docs</a></b>'


def test_headings_become_bold() -> None:
    assert markdown_to_telegram_html("# Title") == "<b>Title</b>"
    assert markdown_to_telegram_html("###### Deep") == "<b>Deep</b>"


def test_heading_marker_mid_line_is_not_a_heading() -> None:
    assert markdown_to_telegram_html("a # b") == "a # b"


def test_heading_without_space_is_not_a_heading() -> None:
    assert markdown_to_telegram_html("#NoSpace") == "#NoSpace"


def test_consecutive_quotes_form_one_blockquote() -> None:
    text = "> one\n> two"
    assert markdown_to_telegram_html(text) == "<blockquote>one\ntwo</blockquote>"


def test_bullets_become_dot_markers() -> None:
    text = "- one\n* two"
    assert markdown_to_telegram_html(text) == "• one\n• two"


def test_numbered_list_is_left_as_is() -> None:
    text = "1. one\n2. two"
    assert markdown_to_telegram_html(text) == "1. one\n2. two"


def test_unmatched_bold_marker_stays_literal() -> None:
    assert markdown_to_telegram_html("2 ** 3 = 8") == "2 ** 3 = 8"


def test_double_newline_is_preserved() -> None:
    assert markdown_to_telegram_html("a\n\nb") == "a\n\nb"


def test_split_keeps_short_text_as_is() -> None:
    assert split_html_safe("<b>hi</b>", SPLIT_LIMIT) == ["<b>hi</b>"]


def test_split_prefers_newline_boundaries() -> None:
    chunks = split_html_safe("aaa\nbbb\ncccddd", 10)
    assert chunks == ["aaa\nbbb", "cccddd"]


def test_split_prefers_word_boundaries() -> None:
    chunks = split_html_safe("aaa bbb ccc ddd", 10)
    assert chunks == ["aaa bbb", "ccc ddd"]


def test_split_long_pre_stays_balanced_and_within_limit() -> None:
    html = "<pre>" + "x" * 120 + "</pre>"

    chunks = split_html_safe(html, SPLIT_LIMIT)

    assert len(chunks) > 1
    assert all(len(chunk) <= SPLIT_LIMIT for chunk in chunks)
    for chunk in chunks:
        assert_balanced(chunk)
    assert "".join(strip_tags(chunk) for chunk in chunks) == "x" * 120


def test_split_closes_and_reopens_formatting_across_chunks() -> None:
    html = "<b>" + "word " * 20 + "</b>"

    chunks = split_html_safe(html, SPLIT_LIMIT)

    assert len(chunks) > 1
    assert all(len(chunk) <= SPLIT_LIMIT for chunk in chunks)
    for chunk in chunks:
        assert_balanced(chunk)
        assert "<b>" in chunk


def test_split_does_not_cut_inside_a_tag() -> None:
    limit = 40
    link = '<a href="https://e.co">label</a>'
    html = "aaaa bbbb " + link + " cccc" * 20

    chunks = split_html_safe(html, limit)

    assert all(len(chunk) <= limit for chunk in chunks)
    assert any(link in chunk for chunk in chunks)
    for chunk in chunks:
        assert_balanced(chunk)
