"""Typed taxonomy of LLM call failures.

Classification maps HTTP statuses and provider error payloads onto a small
set of error kinds; only transient kinds are worth retrying (see
`RetryingLLMClient` in `llm/retry.py`).
"""

from enum import StrEnum
from http import HTTPStatus

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
    """Base class for typed LLM call failures."""

    kind: ErrorKind

    @property
    def transient(self) -> bool:
        """Whether the failure is worth retrying."""
        return self.kind in TRANSIENT_KINDS


class RateLimitError(LLMError):
    """The provider throttled the request (HTTP 429)."""

    kind = ErrorKind.RATE_LIMIT

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
    """Map an HTTP status and a parsed error body onto the typed taxonomy."""
    code, message = _extract_error_fields(body)
    text = f"{code} {message}".lower()
    detail = message or f"{DEFAULT_ERROR_MESSAGE} (HTTP {status})"
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return RateLimitError(detail, retry_after=retry_after)
    if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return AuthError(detail)
    if status == HTTPStatus.PAYMENT_REQUIRED or any(marker in text for marker in _QUOTA_MARKERS):
        return QuotaError(detail)
    if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
        return ContextOverflowError(detail)
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return ProviderInternalError(detail)
    return ClientError(detail)


def parse_retry_after(raw: str | None) -> float | None:
    """Parse a Retry-After header value (seconds) into a float."""
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


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
