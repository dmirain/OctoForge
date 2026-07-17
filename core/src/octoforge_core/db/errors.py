"""Errors raised by the persistence layer."""


class DialogNotFoundError(Exception):
    """Raised when a dialog id is not present in the store."""
