"""Errors raised by the outbound network subsystem."""


class SsrfBlockedError(Exception):
    """Raised when an outbound URL resolves to a non-public address."""


class ToolSpecError(Exception):
    """Raised when a tool instruction's content is not a valid tool spec."""


class ExternalCallError(Exception):
    """Raised when an external call fails (params validation, upstream errors)."""
