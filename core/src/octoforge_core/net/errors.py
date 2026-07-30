"""Errors raised by the outbound network subsystem."""


class SsrfBlockedError(Exception):
    """Raised when an outbound URL resolves to a non-public address."""


class EgressBlockedError(Exception):
    """Raised when a raw HTTP call targets an origin outside the allowlist.

    Distinct from `SsrfBlockedError`: the address is perfectly routable, the
    installation simply does not permit the agent to talk to it.
    """


class ToolSpecError(Exception):
    """Raised when a tool instruction's content is not a valid tool spec."""


class ExternalCallError(Exception):
    """Raised when an external call fails (params validation, upstream errors)."""
