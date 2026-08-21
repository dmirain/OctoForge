"""Typed requests for archive retrieval."""

from dataclasses import dataclass

from octoforge_core.context.types import ArchiveFilter


@dataclass(frozen=True, slots=True)
class ArchiveSearch:
    dialog_id: str
    query: str
    filters: ArchiveFilter | None
    limit: int


@dataclass(frozen=True, slots=True)
class ArchiveTail:
    dialog_id: str
    after_seq: int
    limit: int | None
