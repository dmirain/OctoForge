"""Render collection query outcomes as agent-facing text."""

import json

from octoforge_core.net.collections.api import (
    CollectionError,
    CollectionNotFoundError,
    Query,
    QueryResult,
)

NOT_FOUND_TEMPLATE = (
    "collection '{ref}' is gone (expired, evicted or never yours): run the call that "
    "produced it again to get fresh data and a fresh ref"
)
RESULT_TEMPLATE = "{rows} of {total} matched (offset {offset})\n{body}"
CUT_HINT = "\n…[cut: narrow the query, page with offset/limit, or raise max_chars]"


def _failure_text(failure: CollectionError, ref: object) -> str:
    if isinstance(failure, CollectionNotFoundError):
        return NOT_FOUND_TEMPLATE.format(ref=ref)
    return f"query refused: {failure}"


def _render_result(query: Query, result: QueryResult, max_chars: int) -> str:
    body = json.dumps(result.rows, ensure_ascii=False, default=str)
    if len(body) > max_chars:
        body = body[:max_chars] + CUT_HINT
    return RESULT_TEMPLATE.format(
        rows=len(result.rows), total=result.total, offset=query.offset, body=body
    )
