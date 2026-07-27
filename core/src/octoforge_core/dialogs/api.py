"""Public boundary of the dialogs module: errors and read-model DTOs."""

from dataclasses import dataclass


class DialogNotFoundError(Exception):
    """Raised when a dialog id does not resolve to a stored dialog."""


@dataclass(frozen=True, slots=True)
class MessageStats:
    """Per-user message counters of one channel (admin/reporting read model)."""

    user_id: str
    message_count: int
    total_chars: int


# `list`-returning signatures below a method named `list` need this alias:
# the method shadows the builtin in class-scope annotations.
MessageStatsList = list[MessageStats]
