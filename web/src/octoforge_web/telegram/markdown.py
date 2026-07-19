"""Pragmatic Markdown to Telegram-HTML conversion and tag-safe splitting.

Only the subset of Markdown that LLMs actually emit is supported: bold, italic,
strikethrough, inline code, fenced code blocks, links, headings, blockquotes and
bullet lists. The output is well-formed HTML limited to the tags Telegram accepts.
"""

import re

BULLET_MARK = "•"
MIN_BOUNDARY_RATIO = 2

_ESCAPE_RE = re.compile(r"[&<>]")
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_FENCE_RE = re.compile(r"^```")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>[ ]?(.*)$")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_INLINE_RE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<bold1>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|~~(?P<strike>.+?)~~"
    r"|\*(?P<italic1>[^*\n]+?)\*"
    r"|(?<!\w)_(?P<italic2>.+?)_(?!\w)"
    r"|(?P<link>\[[^\]]+\]\([^)]+\))"
)
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\"[^\"]*\"|'[^']*'|[^'\">])*>")
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "tg"})
_FORMAT_TAGS = (("bold1", "b"), ("bold2", "b"), ("strike", "s"), ("italic1", "i"), ("italic2", "i"))


def markdown_to_telegram_html(text: str) -> str:
    """Convert the supported Markdown subset into Telegram-compatible HTML."""
    lines = text.split("\n")
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        block, index = _render_block(lines, index)
        rendered.append(block)
    return "\n".join(rendered)


def _render_block(lines: list[str], index: int) -> tuple[str, int]:
    """Render one block starting at `lines[index]`; return it and the next index."""
    line = lines[index]
    if _FENCE_RE.match(line):
        return _render_fence(lines, index)
    quote = _QUOTE_RE.match(line)
    if quote:
        return _render_quote(lines, index)
    heading = _HEADING_RE.match(line)
    if heading:
        return f"<b>{_render_inline(heading.group(1))}</b>", index + 1
    bullet = _BULLET_RE.match(line)
    if bullet:
        return f"{BULLET_MARK} {_render_inline(bullet.group(1))}", index + 1
    return _render_inline(line), index + 1


def _render_fence(lines: list[str], start: int) -> tuple[str, int]:
    """Render a fenced code block; an unclosed fence runs to the end of the text."""
    body: list[str] = []
    index = start + 1
    while index < len(lines) and not _FENCE_RE.match(lines[index]):
        body.append(_escape(lines[index]))
        index += 1
    if index < len(lines):
        index += 1  # skip the closing fence; the language tag is dropped either way
    return "<pre>" + "\n".join(body) + "</pre>", index


def _render_quote(lines: list[str], start: int) -> tuple[str, int]:
    """Render a group of consecutive quote lines as a single blockquote."""
    body: list[str] = []
    index = start
    while index < len(lines):
        quote = _QUOTE_RE.match(lines[index])
        if not quote:
            break
        body.append(_render_inline(quote.group(1)))
        index += 1
    return "<blockquote>" + "\n".join(body) + "</blockquote>", index


def _render_inline(text: str) -> str:
    """Render inline Markdown, HTML-escaping the plain text between constructs."""
    parts: list[str] = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        parts.append(_escape(text[pos : match.start()]))
        parts.append(_render_match(match))
        pos = match.end()
    parts.append(_escape(text[pos:]))
    return "".join(parts)


def _render_match(match: re.Match[str]) -> str:
    code = match.group("code")
    if code is not None:
        return f"<code>{_escape(code)}</code>"
    for name, tag in _FORMAT_TAGS:
        inner = match.group(name)
        if inner is not None:
            return f"<{tag}>{_render_inline(inner)}</{tag}>"
    link = match.group("link")
    if link is not None:
        return _render_link(link)
    return _escape(match.group(0))


def _render_link(raw: str) -> str:
    link = _LINK_RE.fullmatch(raw)
    if link is None:
        return _escape(raw)
    label, url = link.group(1), link.group(2)
    if url.split(":", 1)[0].lower() not in _ALLOWED_LINK_SCHEMES:
        return _escape(raw)  # unsupported scheme: keep the literal text
    href = _escape(url).replace('"', "&quot;")
    return f'<a href="{href}">{_render_inline(label)}</a>'


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda match: _ESCAPES[match.group()], text)


def split_html_safe(html: str, limit: int) -> list[str]:
    """Split well-formed HTML into chunks of at most `limit` chars.

    Cuts prefer line, then word boundaries; a cut never lands inside a tag.
    Tags left open at a cut are closed at the end of the head chunk and
    reopened at the start of the tail, so every chunk stays balanced.
    """
    if len(html) <= limit:
        return [html]
    chunks: list[str] = []
    rest = html
    while len(rest) > limit:
        cut = _find_cut(rest, limit)
        head, closing = _close_within_limit(rest, cut, limit)
        open_tags = _open_tags(head)
        tail = "".join(raw for _, raw in open_tags) + _drop_boundary(rest[len(head) :])
        chunks.append(head + closing)
        rest = tail
    chunks.append(rest)
    return chunks


def _close_within_limit(text: str, cut: int, limit: int) -> tuple[str, str]:
    """Back the cut off until the head plus its closing tags fit into `limit`."""
    while True:
        head = text[:cut]
        closing = "".join(f"</{name}>" for name, _ in reversed(_open_tags(head)))
        if len(head) + len(closing) <= limit:
            return head, closing
        cut = _tag_safe_cut(text, _shrink_cut(text, cut))


def _open_tags(html: str) -> list[tuple[str, str]]:
    """Tags still open at the end of `html`, as (name, raw opening tag) pairs."""
    stack: list[tuple[str, str]] = []
    for match in _TAG_RE.finditer(html):
        if match.group(1):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == match.group(2):
                    del stack[index:]
                    break
        else:
            stack.append((match.group(2), match.group(0)))
    return stack


def _find_cut(text: str, limit: int) -> int:
    for separator in ("\n", " "):
        cut = text.rfind(separator, 0, limit)
        if cut >= limit // MIN_BOUNDARY_RATIO:
            return _tag_safe_cut(text, cut)
    return _tag_safe_cut(text, limit)


def _tag_safe_cut(text: str, cut: int) -> int:
    """Move a cut that lands inside a tag to just before that tag.

    A tag starting at the very beginning and spanning the cut is a pathological
    case (an opening tag wider than half the limit); the cut is kept as is and
    the API fallback to plain text covers it.
    """
    tag_start = text.rfind("<", 0, cut)
    if tag_start > text.rfind(">", 0, cut) and tag_start > 0:
        return tag_start
    return cut


def _shrink_cut(text: str, cut: int) -> int:
    for separator in ("\n", " "):
        previous = text.rfind(separator, 0, cut)
        if previous > 0:
            return previous
    return cut - 1


def _drop_boundary(text: str) -> str:
    if text[:1] in ("\n", " "):
        return text[1:]
    return text
