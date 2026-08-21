"""Context summaries, assembled branches and archived messages."""

from dataclasses import dataclass
from datetime import datetime

from octoforge_core.domain import ChatMessage, MessageRole

NO_COMPACTED_SEQ = 0
INTERRUPTED_NOTE = "[The previous assistant message was interrupted and may be incomplete.]"


@dataclass(frozen=True, slots=True)
class DialogueSummary:
    id: str
    dialog_id: str
    seq_from: int
    seq_to: int
    topics: tuple[str, ...]
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AssembledContext:
    messages: list[ChatMessage]
    tail_count: int
    snapshot_len: int


@dataclass(frozen=True, slots=True)
class ArchivedMessage:
    seq: int
    role: MessageRole
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ArchiveFilter:
    seq_ranges: tuple[tuple[int, int], ...] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
