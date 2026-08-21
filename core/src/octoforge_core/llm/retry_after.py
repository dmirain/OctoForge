"""Parsing of provider Retry-After hints."""

from datetime import UTC
from email.utils import parsedate_to_datetime

from octoforge_core.time import utc_now


def parse_retry_after(raw: str | None) -> float | None:
    """Parse Retry-After seconds or an HTTP date."""
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return _parse_http_date(raw.strip())
    return value if value >= 0 else None


def _parse_http_date(raw: str) -> float | None:
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - utc_now()).total_seconds())
