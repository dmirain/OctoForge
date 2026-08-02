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
    closed, but nobody must restart it. COLLECTING means work for NOBODY yet:
    forwarded material is accumulating and the reaction is deferred until the
    user asks something or the collection falls quiet.
    """

    COLLECTING = "collecting"
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    AWAITING_USER = "awaiting_user"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: statuses an incoming message may still be routed into
LIVE_EXCHANGE_STATUSES = (
    ExchangeStatus.COLLECTING,
    ExchangeStatus.OPEN,
    ExchangeStatus.IN_PROGRESS,
    ExchangeStatus.AWAITING_USER,
)

#: how long an exchange title may be — it is a label (operator console, the
#: nudge text, the router's candidate lines), not the question itself, which
#: stays whole in the message. The store clamps to it; whoever writes a title
#: should aim under it rather than rely on the cut.
TITLE_MAX_LENGTH = 60


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
    pending_question: str | None = None


ExchangeList = list[Exchange]


class ExchangeRepository(Protocol):
    """Port over the exchanges of a dialog."""

    async def create(
        self,
        dialog_id: str,
        title: str,
        status: ExchangeStatus | None = None,
    ) -> Exchange:
        """Open a new exchange, OPEN unless told otherwise.

        `status` sets the first state in the same write: a collecting exchange
        must never exist as OPEN-and-unowned, not even briefly, or the
        recovery sweep would grab it as work to do.
        """
        ...

    async def get(self, exchange_id: str) -> Exchange:
        """Return the exchange or raise ExchangeNotFoundError."""
        ...

    async def list_live(self, dialog_id: str) -> ExchangeList:
        """Return the dialog's non-terminal exchanges, oldest first."""
        ...

    async def find_collecting(self, dialog_id: str) -> Exchange | None:
        """Return the dialog's collecting exchange, None when there is none.

        At most one exists per dialog: material joins it instead of opening a
        second collection, so one forward burst yields one reaction.
        """
        ...

    async def list_stale_collecting(self, quiet_seconds: float) -> ExchangeList:
        """Collecting exchanges untouched for longer than `quiet_seconds`.

        The promotion predicate of the sweep: the batch has fallen quiet, so
        it is time to react to it.
        """
        ...

    async def touch(self, exchange_id: str) -> None:
        """Bump `updated_at` — the clock the quiet window is measured against."""
        ...

    async def set_title(self, exchange_id: str, title: str) -> None:
        """Rename the exchange; a missing row is a no-op.

        An exchange is named when it opens, from the message that opened it —
        after a few turns that name describes its first sentence rather than
        the obligation. Routing renames it whenever a message joins, so the
        name keeps up with what the exchange became.
        """
        ...

    async def list_unowned_open(self, dialog_id: str | None = None) -> ExchangeList:
        """OPEN exchanges without an owner, oldest first (None: all dialogs).

        The "work left for the system" predicate: every crash or limit
        window that leaves a question unowned lands here, and the sweeps
        (startup, freed slot) revive them.
        """
        ...

    async def list_stranded_dialog_ids(self) -> list[str]:
        """Dialog ids holding an exchange no live run can finish on its own.

        IN_PROGRESS (its executor lives in some process's memory) or
        OPEN-and-unowned (nobody picked it up). Recovery starts from this
        list and then asks who, if anyone, still owns those dialogs.
        """
        ...

    async def reopen_and_list_stranded(self, dialog_id: str) -> tuple[int, ExchangeList]:
        """Reset the dialog's IN_PROGRESS exchanges to OPEN; return `(reopened, stranded)`.

        `stranded` is the dialog's OPEN exchanges without a live task, the
        just-reopened ones included — recovery always asks both questions
        together, so they share one transaction.

        Scoped to one dialog on purpose. The reset used to touch every row in
        the database on the reasoning that a restart kills every process —
        true of one process, and catastrophic with two: a starting instance
        would reset exchanges its peers are running right now. The caller
        must establish that nobody live owns the dialog (`held_elsewhere`)
        before calling this.
        """
        ...

    async def set_status(
        self,
        exchange_id: str,
        status: ExchangeStatus,
        pending_question: str | None = None,
    ) -> None:
        """Move the exchange to `status`, replacing the pending question."""
        ...

    async def settle_owned(
        self,
        exchange_id: str,
        task_id: str,
        status: ExchangeStatus,
        *,
        keep_if_awaiting: bool = False,
    ) -> Exchange | None:
        """Set the status while `task_id` is the exchange's newest task and it is
        still live; None when nothing was written."""
        ...

    async def delete_for_dialog(self, dialog_id: str) -> None:
        """Drop every exchange of the dialog (admin dialog deletion)."""
        ...


@dataclass(frozen=True, slots=True)
class DialogClaim:
    """A process's claim on running one dialog's actor.

    `generation` is the whole guard: the owner remembers the number it was
    given and compares it against the stored one. A higher number means
    somebody else took the dialog, and this owner must stop — no message
    between the two processes is needed, which is what makes the check work
    when the previous owner is unreachable or already dead.
    """

    dialog_id: str
    owner: str
    generation: int
    heartbeat_at: datetime


#: `list`-shadowing alias, same reason as the others in this module
DialogClaimList = list[DialogClaim]


class ClaimRepository(Protocol):
    """Port over dialog ownership: who runs which actor, and since when.

    Exists so a second process is safe. With one process every claim
    succeeds, nothing is ever preempted, and the recovery filter matches
    everything — the behavior is exactly what it was before claims existed.
    """

    async def claim(self, dialog_id: str, owner: str) -> DialogClaim:
        """Take the dialog for `owner`, bumping its generation; never fails.

        Deliberately unconditional: placement is the router's decision, not
        the database's. Taking a dialog somebody else holds is a legitimate
        handover, and the loser finds out through its own generation check.
        """
        ...

    async def heartbeat(self, claims: DialogClaimList) -> frozenset[str]:
        """Refresh the given claims; return the ids still held by their owner.

        A dialog missing from the result was preempted (or its row is gone),
        which is how a live owner learns it must stand down.
        """
        ...

    async def release(self, dialog_id: str, owner: str, generation: int) -> None:
        """Drop the claim if it is still this exact one; otherwise a no-op.

        The generation check matters on shutdown: a dialog already taken over
        by another process must keep ITS claim, not lose it to our exit.
        """
        ...

    async def held_elsewhere(
        self, dialog_ids: frozenset[str], owner: str, stale_before: datetime
    ) -> frozenset[str]:
        """Of `dialog_ids`, those a DIFFERENT live owner holds.

        The recovery filter. "Live" means a heartbeat at or after
        `stale_before`; our own claims are never returned, because a process
        that is only now starting up cannot be running them.
        """
        ...

    async def current_generation(self, dialog_id: str) -> int | None:
        """The stored generation, or None when nobody holds the dialog."""
        ...

    async def delete_for_dialog(self, dialog_id: str) -> None:
        """Drop the dialog's claim (admin dialog deletion)."""
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

    async def list_hot_slice(self, dialog_id: str) -> list[ChatMessage]:
        """Return the messages past the dialog's compaction boundary, ordered by seq."""
        ...

    async def last_activity_by_channel(self, channel: str) -> dict[str, datetime]:
        """When each person of the channel last had a message, keyed by person."""
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
