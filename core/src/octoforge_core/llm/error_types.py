"""Typed taxonomy of failures returned by LLM providers."""

from enum import StrEnum


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


class LLMError(Exception):
    """Base class for typed provider failures, with an optional retry hint."""

    kind: ErrorKind

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after

    @property
    def transient(self) -> bool:
        return self.kind in TRANSIENT_KINDS


class RateLimitError(LLMError):
    """The provider throttled the request."""

    kind = ErrorKind.RATE_LIMIT


class AuthError(LLMError):
    """The provider rejected the credentials."""

    kind = ErrorKind.AUTH


class QuotaError(LLMError):
    """The account is out of quota or credit."""

    kind = ErrorKind.QUOTA


class ContextOverflowError(LLMError):
    """The prompt exceeds the model's context window."""

    kind = ErrorKind.CONTEXT_OVERFLOW


class ProviderInternalError(LLMError):
    """The provider failed on its side."""

    kind = ErrorKind.PROVIDER_INTERNAL


class TransportError(LLMError):
    """The request never got a usable HTTP response."""

    kind = ErrorKind.TRANSPORT


class ClientError(LLMError):
    """The request itself is invalid."""

    kind = ErrorKind.CLIENT
