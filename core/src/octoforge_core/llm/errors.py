"""Typed taxonomy of LLM call failures.

Classification maps HTTP statuses and provider error payloads onto a small
set of error kinds; only transient kinds are worth retrying (see
`RetryingLLMClient` in `llm/retry.py`).
"""

from contextlib import suppress
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http import HTTPStatus

import httpx

from octoforge_core.time import utc_now

DEFAULT_ERROR_MESSAGE = "LLM request failed"


class ErrorKind(StrEnum):
    """Classification of LLM call failures."""

    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    QUOTA = "quota"
    CONTEXT_OVERFLOW = "context_overflow"
    PROVIDER_INTERNAL = "provider_internal"
    TRANSPORT = "transport"
    CLIENT = "client"


TRANSIENT_KINDS: frozenset[ErrorKind] = frozenset(
    {ErrorKind.RATE_LIMIT, ErrorKind.PROVIDER_INTERNAL, ErrorKind.TRANSPORT}
)

_CONTEXT_OVERFLOW_MARKERS = (
    "context_length",
    "context_overflow",
    "context window",
    "maximum context",
    "too many tokens",
)
_QUOTA_MARKERS = ("insufficient_quota", "quota_exceeded", "billing")


class LLMError(Exception):
    """Base class for typed LLM call failures.

    `retry_after` carries the provider's Retry-After hint (seconds) when the
    response had one, whatever the error kind is.
    """

    kind: ErrorKind

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        """Whether the failure is worth retrying."""
        return self.kind in TRANSIENT_KINDS


class RateLimitError(LLMError):
    """The provider throttled the request (HTTP 429)."""

    kind = ErrorKind.RATE_LIMIT


class AuthError(LLMError):
    """The provider rejected the credentials (HTTP 401/403)."""

    kind = ErrorKind.AUTH


class QuotaError(LLMError):
    """The account is out of quota or credit; retrying is pointless."""

    kind = ErrorKind.QUOTA


class ContextOverflowError(LLMError):
    """The prompt exceeds the model's context window."""

    kind = ErrorKind.CONTEXT_OVERFLOW


class ProviderInternalError(LLMError):
    """The provider failed on its side (HTTP 5xx)."""

    kind = ErrorKind.PROVIDER_INTERNAL


class TransportError(LLMError):
    """The request never got a usable HTTP response (DNS, connect, timeout)."""

    kind = ErrorKind.TRANSPORT


class ClientError(LLMError):
    """The request itself is invalid (other HTTP 4xx)."""

    kind = ErrorKind.CLIENT


def classify_http_error(status: int, body: object, retry_after: float | None) -> LLMError:
    """Map an HTTP status and a parsed error body onto the typed taxonomy.

    Body markers win over the status: providers report exhausted quota as a
    bare 429, and the payload tells throttling apart from a fatal quota
    failure that must not be retried.
    """
    code, message = _extract_error_fields(body)
    text = f"{code} {message}".lower()
    detail = message or f"{DEFAULT_ERROR_MESSAGE} (HTTP {status})"
    error_type: type[LLMError] = ClientError
    if any(marker in text for marker in _QUOTA_MARKERS):
        error_type = QuotaError
    elif any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
        error_type = ContextOverflowError
    elif status == HTTPStatus.TOO_MANY_REQUESTS:
        error_type = RateLimitError
    elif status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        error_type = AuthError
    elif status == HTTPStatus.PAYMENT_REQUIRED:
        error_type = QuotaError
    elif status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        error_type = ProviderInternalError
    return error_type(detail, retry_after=retry_after)


def parse_retry_after(raw: str | None) -> float | None:
    """Parse a Retry-After header value (seconds or HTTP-date) into a float."""
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return _parse_http_date(raw.strip())
    return value if value >= 0 else None


def raise_for_error_status(response: httpx.Response) -> None:
    """Raise a typed LLMError when the response carries an error status.

    Safe on a streaming response whose body was never read: the provider's
    message is then unavailable, so the status alone decides the kind. Prefer
    `araise_for_error_status` there — it reads the body first.
    """
    if response.status_code < HTTPStatus.BAD_REQUEST:
        return
    try:
        body: object = response.json()
    except (ValueError, httpx.ResponseNotRead):
        # ResponseNotRead is a RuntimeError, not an HTTPError: left uncaught it
        # escaped the client untyped, and the retry layer (which only knows
        # LLMError) never saw a rate limit that arrived at stream start.
        body = None
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    raise classify_http_error(response.status_code, body, retry_after)


async def araise_for_error_status(response: httpx.Response) -> None:
    """Same, for a streaming response: read the error body before classifying.

    An error ends the stream anyway, so reading it costs nothing and keeps the
    provider's own message ("you exceeded your quota") in the error text.
    """
    if response.status_code < HTTPStatus.BAD_REQUEST:
        return
    with suppress(httpx.HTTPError):
        await response.aread()
    raise_for_error_status(response)


def _parse_http_date(raw: str) -> float | None:
    """Parse the HTTP-date form of Retry-After into seconds from now."""
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - utc_now()).total_seconds())


def _extract_error_fields(body: object) -> tuple[str, str]:
    """Pull the error code and message out of an OpenAI-style error payload."""
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error")
    if not isinstance(error, dict):
        return "", ""
    code = error.get("code") or error.get("type") or ""
    message = error.get("message") or ""
    return str(code), str(message)
