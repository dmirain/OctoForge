"""Normalization of wire-level admin paging values."""

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


def clamp_page(limit: int | None, offset: int | None) -> tuple[int, int]:
    resolved_limit = DEFAULT_PAGE_SIZE if limit is None else max(1, min(limit, MAX_PAGE_SIZE))
    resolved_offset = max(0, offset or 0)
    return resolved_limit, resolved_offset
