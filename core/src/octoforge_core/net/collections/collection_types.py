"""Collection identity, storage metadata and configuration."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_MAX_PER_USER = 20
DEFAULT_MAX_MB_PER_USER = 50
DEFAULT_QUERY_LIMIT = 50
DEFAULT_QUERY_MAX_LIMIT = 500
DEFAULT_INLINE_MAX_CHARS = 2000
DEFAULT_COLLECT_MAX_PAGES = 20
DEFAULT_QUERY_DEFAULT_CHARS = 8000
DEFAULT_QUERY_MAX_CHARS = 32_000
REF_PREFIX = "col:"


class CollectionKind(StrEnum):
    """Source-body shape persisted with the collection."""

    JSON = "json"
    CSV = "csv"
    DOC_JSON = "doc_json"
    DOC_TEXT = "doc_text"


@dataclass(frozen=True, slots=True)
class CollectionPassport:
    """Model-facing metadata for one stored collection."""

    id: str
    owner_id: str
    label: str
    kind: CollectionKind
    source: str
    schema: dict[str, Any]
    envelope: dict[str, Any]
    record_count: int
    byte_size: int
    pages_loaded: int
    truncated: bool
    created_at: datetime
    expires_at: datetime

    @property
    def ref(self) -> str:
        return f"{REF_PREFIX}{self.id}"


@dataclass(frozen=True, slots=True)
class CollectionConfig:
    """Behavior knobs mapped from deployment settings."""

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    max_per_user: int = DEFAULT_MAX_PER_USER
    max_bytes_per_user: int = DEFAULT_MAX_MB_PER_USER * 1024 * 1024
    query_default_limit: int = DEFAULT_QUERY_LIMIT
    query_max_limit: int = DEFAULT_QUERY_MAX_LIMIT
    inline_max_chars: int = DEFAULT_INLINE_MAX_CHARS
    collect_max_pages: int = DEFAULT_COLLECT_MAX_PAGES
    query_default_chars: int = DEFAULT_QUERY_DEFAULT_CHARS
    query_max_chars: int = DEFAULT_QUERY_MAX_CHARS


@dataclass(frozen=True, slots=True)
class NewRecords:
    """One ordered batch entering a collection."""

    payloads: list[dict[str, Any]]
    source: str = ""
