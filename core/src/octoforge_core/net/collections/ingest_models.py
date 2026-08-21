"""Immutable requests and parsed values for collection ingestion."""

from dataclasses import dataclass
from typing import Any

from octoforge_core.net.collections.api import CollectionKind, NewRecords
from octoforge_core.net.tool_spec import ResponseSpec


@dataclass(frozen=True, slots=True)
class SpillRequest:
    """One already-scrubbed response offered to the spill router."""

    owner_id: str
    body: str
    content_type: str
    source: str
    wire_truncated: bool
    label: str = ""
    scope: str = ""
    response: ResponseSpec | None = None


@dataclass(frozen=True, slots=True)
class ParsedBody:
    """A structured body split into records and its surrounding document."""

    kind: CollectionKind
    records: NewRecords
    envelope: dict[str, Any]
    document: object = None
    single_document: bool = False
    record_truncated: bool = False
