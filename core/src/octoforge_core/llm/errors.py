"""Classify HTTP provider failures into the public typed LLM taxonomy."""

from contextlib import suppress
from http import HTTPStatus

import httpx

from octoforge_core.llm.error_types import (
    AuthError,
    ClientError,
    ContextOverflowError,
    ErrorKind,
    LLMError,
    ProviderInternalError,
    QuotaError,
    RateLimitError,
    TransportError,
)
from octoforge_core.llm.retry_after import parse_retry_after

DEFAULT_ERROR_MESSAGE = "LLM request failed"
CONTEXT_OVERFLOW_MARKERS = (
    "context_length",
    "context_overflow",
    "context window",
    "maximum context",
    "too many tokens",
)
QUOTA_MARKERS = ("insufficient_quota", "quota_exceeded", "billing")

__all__ = [
    "AuthError",
    "ClientError",
    "ContextOverflowError",
    "ErrorKind",
    "LLMError",
    "ProviderInternalError",
    "QuotaError",
    "RateLimitError",
    "TransportError",
    "araise_for_error_status",
    "classify_http_error",
    "parse_retry_after",
    "raise_for_error_status",
]


def classify_http_error(status: int, body: object, retry_after: float | None) -> LLMError:
    """Map status and provider payload onto the typed taxonomy."""
    code, message = _extract_error_fields(body)
    text = f"{code} {message}".lower()
    detail = message or f"{DEFAULT_ERROR_MESSAGE} (HTTP {status})"
    error_type: type[LLMError] = ClientError
    if any(marker in text for marker in QUOTA_MARKERS):
        error_type = QuotaError
    elif any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS):
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


def raise_for_error_status(response: httpx.Response) -> None:
    """Raise a typed error when a buffered response has an error status."""
    if response.status_code < HTTPStatus.BAD_REQUEST:
        return
    try:
        body: object = response.json()
    except (ValueError, httpx.ResponseNotRead):
        body = None
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    raise classify_http_error(response.status_code, body, retry_after)


async def araise_for_error_status(response: httpx.Response) -> None:
    """Read a streaming error body before classifying it."""
    if response.status_code < HTTPStatus.BAD_REQUEST:
        return
    with suppress(httpx.HTTPError):
        await response.aread()
    raise_for_error_status(response)


def _extract_error_fields(body: object) -> tuple[str, str]:
    if not isinstance(body, dict):
        return "", ""
    error = body.get("error")
    if not isinstance(error, dict):
        return "", ""
    code = error.get("code") or error.get("type") or ""
    message = error.get("message") or ""
    return str(code), str(message)
