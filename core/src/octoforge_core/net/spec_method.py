"""Allowed HTTP methods of stored endpoint contracts."""

from octoforge_core.net.errors import ToolSpecError

ALLOWED_METHODS = (
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "HEAD",
    "OPTIONS",
    "PROPFIND",
    "PROPPATCH",
    "REPORT",
    "MKCOL",
    "MKCALENDAR",
    "COPY",
    "MOVE",
)


def parse_method(raw: object) -> str:
    if not isinstance(raw, str):
        raise ToolSpecError("method must be a string")
    method = raw.upper()
    if method not in ALLOWED_METHODS:
        raise ToolSpecError(f"unsupported method: {raw!r}")
    return method
