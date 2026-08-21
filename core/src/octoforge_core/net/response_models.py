"""Types and constants shared by remembered-response modules."""

from dataclasses import dataclass
from typing import Protocol

REF_PREFIX = "resp:"

DEFAULT_MAX_RESPONSE_MB = 2
DEFAULT_BUDGET_MB = 200
DEFAULT_GET_CHARS = 8000
DEFAULT_GET_MAX_CHARS = 100_000
RENDER_IN_THREAD_CHARS = 256 * 1024
MEGABYTE = 1024 * 1024

NOT_FOUND_TEMPLATE = (
    "response '{ref}' is gone (expired, swept or never yours): run the call "
    "again for fresh data and a fresh ref"
)
NO_KEY_TEMPLATE = "key '{key}' is not in this response; it has: {known}"
TEXT_HAS_NO_KEYS = "this response is plain text; call without a key"


class ResponseNotFoundError(Exception):
    """The ref names no live response of this owner."""


@dataclass(frozen=True, slots=True)
class ResponseMemoryConfig:
    """Behavior knobs, mapped from the deployment's settings."""

    max_response_chars: int = DEFAULT_MAX_RESPONSE_MB * MEGABYTE
    budget_chars: int = DEFAULT_BUDGET_MB * MEGABYTE
    get_default_chars: int = DEFAULT_GET_CHARS
    get_max_chars: int = DEFAULT_GET_MAX_CHARS


@dataclass(frozen=True, slots=True)
class DocumentDraft:
    """One document and its ownership, lifetime scope, and provenance."""

    owner_id: str
    scope: str
    source: str
    body: str
    document: object = None

    @property
    def kind(self) -> str:
        """The reading kind implied by whether parsed JSON is present."""
        return "json" if self.document is not None else "text"


@dataclass(slots=True)
class StoredResponse:
    """One RAM-parked document and its LRU metadata."""

    id: str
    owner_id: str
    scope: str
    kind: str
    source: str
    body: str
    document: object = None
    last_access: float = 0.0

    @property
    def ref(self) -> str:
        return f"{REF_PREFIX}{self.id}"


@dataclass(frozen=True, slots=True)
class StoredDocument:
    """One parked document as the reading tools see it, wherever it lives."""

    ref: str
    kind: str
    source: str
    body: str
    document: object = None


class DocumentHome(Protocol):
    """Storage seam for documents too large for the context."""

    async def park(self, draft: DocumentDraft) -> str:
        """Store the document and answer its passport text."""
        ...

    async def fetch(self, owner_id: str, ref: str) -> StoredDocument:
        """Return the document, or raise `ResponseNotFoundError`."""
        ...
