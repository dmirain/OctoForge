"""Public boundary of the dialogs module: ports, errors and read-model DTOs.

The ports mirror the convention of every other module (TaskStore,
InstructionStore, ...): consumers type against the Protocol, the SQL
implementations live in `store.py`, and tests substitute fakes without
subclassing concrete classes (the 2026-07-27 audit, item 5 — the dialog
repositories were the last concrete-only dependencies in the actor).
"""

from dataclasses import dataclass
from typing import Protocol

from octoforge_core.domain import ChatMessage, Dialog
from octoforge_core.llm.usage import Usage


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


# `list`-shadowing alias for signatures declared after a `list` method
ChatMessageList = list[ChatMessage]


class DialogRepository(Protocol):
    """Port over the dialog registry keyed by the unique (user_id, channel)."""

    async def get_or_create(self, user_id: str, channel: str) -> Dialog:
        """Return the dialog for (user_id, channel), creating it on first contact."""
        ...

    async def get(self, dialog_id: str) -> Dialog:
        """Return the dialog by id or raise DialogNotFoundError."""
        ...

    async def list_user_ids_by_channel(self, channel: str) -> list[str]:
        """Return the user ids that have a dialog on the given channel."""
        ...

    async def list_by_channel(self, channel: str) -> list[Dialog]:
        """Return the full dialogs of the given channel."""
        ...

    async def delete(self, dialog_id: str) -> None:
        """Delete the dialog and its message log in one transaction.

        Only this module's tables go: rows of other modules referencing the
        dialog (tasks, summaries) are their owners' business — the admin
        deletion surface composes the per-module deletes. Raises
        `DialogNotFoundError` when the id is unknown.
        """
        ...


class MessageRepository(Protocol):
    """Port over the ordered message log of a dialog (seq grows monotonically)."""

    async def append(
        self,
        dialog_id: str,
        message: ChatMessage,
        usage: Usage | None = None,
        client_message_id: str | None = None,
    ) -> str:
        """Append a message with the next seq; return the row id."""
        ...

    async def append_pair(self, dialog_id: str, first: ChatMessage, second: ChatMessage) -> None:
        """Append two messages atomically with consecutive seq values."""
        ...

    async def find_by_client_id(self, dialog_id: str, client_message_id: str) -> bool:
        """Whether a message with this idempotency key is already recorded."""
        ...

    # list_after is declared above `list`: the latter shadows the builtin in
    # class-scope annotations (same ordering rule as the implementation)
    async def list_after(self, dialog_id: str, after_seq: int) -> list[ChatMessage]:
        """Return the messages with seq strictly above `after_seq`, ordered by seq."""
        ...

    async def list(self, dialog_id: str) -> ChatMessageList:
        """Return the dialog messages ordered by seq."""
        ...

    async def stats_by_channel(self, channel: str) -> MessageStatsList:
        """Return per-user message counters of the channel."""
        ...
