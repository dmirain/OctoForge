"""Public boundary of the context module.

Everything the rest of the system (the dialog actor, tools, the composition
root) may know about context compaction lives here: the `ContextCompactor`
port, the `SummaryStore` port (compressed segments of the history), the
`MessageArchive` port (read access to the full message log) and the
JSON-serializable DTOs.

The three context levels: the archive (`messages` table, written by the
actor), the topics (`dialog_summaries`, written by the compactor) and the hot
tail (archive messages with `seq > max(seq_to)`, derived from the data — no
separate "compacted up to" pointer, so the state survives any restart).

The protocols are deliberately transport-shaped: the DTOs contain only
JSON-compatible fields (datetimes serialize as ISO 8601 at a wire boundary),
so a future HTTP implementation is the planned "extract to a dedicated
service" path — call sites will not change.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from octoforge_core.domain import ChatMessage, Dialog, MessageRole

NO_COMPACTED_SEQ = 0
INTERRUPTED_NOTE = "[The previous assistant message was interrupted and may be incomplete.]"


@dataclass(frozen=True, slots=True)
class DialogueSummary:
    """One compressed segment of a dialog's archive with topic tags.

    JSON-friendly: str/int fields, a tuple of tag strings and a UTC datetime
    (ISO 8601 at a wire boundary). `[seq_from, seq_to]` is the covered
    inclusive range of archive seq values.
    """

    id: str
    dialog_id: str
    seq_from: int
    seq_to: int
    topics: tuple[str, ...]
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """Result of assembling a branch: the messages plus the hot-tail size.

    `tail_count` is how many trailing narrative messages went in verbatim —
    the actor uses it to trim its in-memory narrative down to the hot tail
    once compaction has advanced (everything older is reachable through
    summaries and history_search only).
    """

    messages: list[ChatMessage]
    tail_count: int


@dataclass(frozen=True, slots=True)
class ArchivedMessage:
    """One message of the archive with its seq (unlike ChatMessage)."""

    seq: int
    role: MessageRole
    content: str
    created_at: datetime


class ContextCompactor(Protocol):
    """Port assembling a process branch from the dialog narrative.

    Implementations: `LlmContextCompactor` (topics block + hot tail, with
    background LLM compaction) and `NoopContextCompactor` (passthrough, the
    default for tests). The implementation is chosen in the composition root.
    """

    async def assemble(self, dialog: Dialog, history: list[ChatMessage]) -> AssembledContext:
        """Return the narrative part of a process branch: topics block + hot tail.

        `history` is the actor's in-memory narrative, a mirror of the hot
        slice of the archive (the actor persists a message before appending
        it). Triggering background compaction on overflow is the
        implementation's business; assemble itself never blocks on it.
        The reported `tail_count` lets the actor drop compacted messages
        from memory.
        """
        ...

    async def compacted_boundary(self, dialog_id: str) -> int:
        """Return the highest archive seq covered by compaction (0 when none).

        The actor's initial narrative load starts right after this boundary:
        older messages live as summaries and through history_search, not in
        memory.
        """
        ...

    async def compact_now(self, dialog: Dialog) -> bool:
        """Compact synchronously (the reactive path after a context overflow).

        Returns True when a new summary was written and re-assembling the
        branch is worthwhile; False when there was nothing to compact (or the
        implementation never compacts — the Noop default).
        """
        ...

    async def aclose(self, dialog_id: str) -> None:
        """Cancel pending background work of the dialog (its runner is stopping).

        Scoped to one dialog: the compactor instance is shared by all runners
        of the manager, so closing must not touch other dialogs' work.
        """
        ...


class SummaryStore(Protocol):
    """Port of the summaries storage (level 2: compressed topics)."""

    async def list_for_dialog(self, dialog_id: str) -> list[DialogueSummary]:
        """Return all summaries of the dialog, ordered by seq_from."""
        ...

    async def replace_for_dialog(self, dialog_id: str, summary: DialogueSummary) -> None:
        """Replace all summaries of the dialog with the one (rolling merge).

        The merged summary covers `[min(previous seq_from), new seq_to]`, so
        `max_seq_to` keeps working and the topics block stays constant-size.
        """
        ...

    async def max_seq_to(self, dialog_id: str) -> int:
        """Return the highest covered seq; NO_COMPACTED_SEQ when nothing was compacted."""
        ...

    async def find_by_topic(self, dialog_id: str, topic: str) -> list[DialogueSummary]:
        """Return the dialog's summaries whose tags match the topic (case-insensitive)."""
        ...


@dataclass(frozen=True, slots=True)
class ArchiveFilter:
    """Optional restrictions of an archive search; a None field is unrestricted.

    `seq_ranges` restricts hits to the inclusive seq ranges (an empty tuple
    matches nothing). `date_from` is inclusive, `date_to` exclusive.
    """

    seq_ranges: tuple[tuple[int, int], ...] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


class MessageArchive(Protocol):
    """Read port over the full message archive (level 1), scoped to one dialog."""

    async def count_after(self, dialog_id: str, seq: int) -> int:
        """Return how many messages the dialog has with `seq >` the given one."""
        ...

    async def tail_after(self, dialog_id: str, seq: int) -> list[ArchivedMessage]:
        """Return the dialog messages with `seq >` the given one, ordered by seq."""
        ...

    async def latest_prompt_tokens(self, dialog_id: str, after_seq: int) -> int | None:
        """Return prompt_tokens of the newest assistant message past the boundary.

        Only messages with `seq > after_seq` count: usage recorded before the
        compaction boundary describes a pre-compaction prompt and must not
        retrigger compaction. None when no tail assistant message carries
        provider usage (the chars heuristic stays the fallback trigger then).
        """
        ...

    async def search(
        self,
        dialog_id: str,
        query: str,
        *,
        filters: ArchiveFilter | None = None,
        limit: int,
    ) -> list[ArchivedMessage]:
        """Return archive messages matching a case-insensitive substring, by seq.

        Only the given dialog is searched (isolation is part of the contract).
        A blank query short-circuits to an empty list.
        """
        ...
