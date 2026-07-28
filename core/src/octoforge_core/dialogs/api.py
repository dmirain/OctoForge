"""Public boundary of the dialogs module: ports, errors and read-model DTOs.

The ports mirror the convention of every other module (TaskStore,
InstructionStore, ...): consumers type against the Protocol, the SQL
implementations live in `store.py`, and tests substitute fakes without
subclassing concrete classes (the 2026-07-27 audit, item 5 — the dialog
repositories were the last concrete-only dependencies in the actor).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from octoforge_core.domain import ChatMessage, Dialog
from octoforge_core.llm.usage import Usage


class DialogNotFoundError(Exception):
    """Raised when a dialog id does not resolve to a stored dialog."""


class ExchangeNotFoundError(Exception):
    """Raised when an exchange id does not resolve to a stored exchange."""


class ExchangeStatus(StrEnum):
    """Lifecycle of one obligation to the user.

    OPEN is the only state that means work for the SYSTEM: the "what is left
    to do" predicate is `OPEN and no owner`, and it also drives restart
    recovery. AWAITING_USER means work for the USER — the exchange is not
    closed, but nobody must restart it.
    """

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_USER = "awaiting_user"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: statuses an incoming message may still be routed into
LIVE_EXCHANGE_STATUSES = (
    ExchangeStatus.OPEN,
    ExchangeStatus.IN_PROGRESS,
    ExchangeStatus.AWAITING_USER,
)


@dataclass(slots=True)
class Exchange:
    """One obligation to the user: their question, clarifications, the answer.

    Deliberately separate from the task row: a run can finish (task DONE)
    without closing the exchange — asking the user something back is exactly
    that case.
    """

    id: str
    dialog_id: str
    status: ExchangeStatus
    title: str
    created_at: datetime
    updated_at: datetime
    owner_task_id: str | None = None
    pending_question: str | None = None


ExchangeList = list[Exchange]


class ExchangeRepository(Protocol):
    """Port over the exchanges of a dialog."""

    async def create(
        self, dialog_id: str, title: str, owner_task_id: str | None = None
    ) -> Exchange:
        """Open a new exchange; IN_PROGRESS when an owner is given, OPEN otherwise."""
        ...

    async def get(self, exchange_id: str) -> Exchange:
        """Return the exchange or raise ExchangeNotFoundError."""
        ...

    async def list_live(self, dialog_id: str) -> ExchangeList:
        """Return the dialog's non-terminal exchanges, oldest first."""
        ...

    async def list_unowned_open(self, dialog_id: str | None = None) -> ExchangeList:
        """OPEN exchanges without an owner, oldest first (None: all dialogs).

        The "work left for the system" predicate: every crash or limit
        window that leaves a question unowned lands here, and the sweeps
        (startup, freed slot) revive them.
        """
        ...

    async def reopen_in_progress(self) -> int:
        """Reset every IN_PROGRESS exchange to OPEN; return how many.

        Startup only: processes never survive a restart, so an owner recorded
        in the database is stale by definition.
        """
        ...

    async def set_status(
        self,
        exchange_id: str,
        status: ExchangeStatus,
        owner_task_id: str | None = None,
        pending_question: str | None = None,
    ) -> None:
        """Move the exchange to `status`, replacing owner and pending question."""
        ...

    async def delete_for_dialog(self, dialog_id: str) -> None:
        """Drop every exchange of the dialog (admin dialog deletion)."""
        ...


@dataclass(frozen=True, slots=True)
class MessageStats:
    """Per-user message counters of one channel, split by author.

    "How much did this person actually write" is the number that matters;
    a summed count buries it under the agent's (usually longer) output.
    """

    user_id: str
    user_messages: int
    user_chars: int
    agent_messages: int
    agent_chars: int


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

    async def set_exchange(self, message_id: str, exchange_id: str) -> None:
        """Attach a stored message to the exchange it belongs to."""
        ...

    async def stats_by_channel(self, channel: str) -> MessageStatsList:
        """Return per-user message counters of the channel."""
        ...
