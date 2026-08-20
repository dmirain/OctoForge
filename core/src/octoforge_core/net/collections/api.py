"""Public boundary of the collections module: DTOs, the DSL, and the ports.

A collection is NOT a table. Two fixed tables hold every collection of every
user; "create a collection" is an INSERT, its schema is a JSON *value* derived
from the data, and no DDL ever runs at runtime. That is what lets a thousand
differently-shaped responses live side by side.

The query DSL is deliberately an object language, not a string language: every
field name is validated against the derived schema before it is compiled, so a
mistake comes back as a readable error naming the fields that do exist — the
same self-correction contract endpoint calls follow.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

DEFAULT_TTL_SECONDS = 3600.0
DEFAULT_MAX_PER_USER = 20
DEFAULT_MAX_MB_PER_USER = 50
DEFAULT_QUERY_LIMIT = 50
DEFAULT_QUERY_MAX_LIMIT = 500
DEFAULT_INLINE_MAX_CHARS = 2000
DEFAULT_COLLECT_MAX_PAGES = 20
DEFAULT_QUERY_DEFAULT_CHARS = 8000
DEFAULT_QUERY_MAX_CHARS = 32_000

#: How a collection is addressed by the model.
REF_PREFIX = "col:"


class CollectionError(Exception):
    """Base of the module's agent-readable failures."""


class CollectionNotFoundError(CollectionError):
    """The ref does not name a live collection of this owner.

    Expiry and eviction land here too: to the caller an expired collection
    and one that never existed are the same answer, with the same remedy —
    fetch the data again.
    """


class CollectionQueryError(CollectionError):
    """The query does not fit the collection (unknown field, wrong type)."""


class CollectionQuotaError(CollectionError):
    """An append would push the owner past the byte quota.

    Only `append` raises it: `create` evicts the least recently used instead,
    but a growing collection must not evict OTHER collections to feed its own
    growth — the collect loop stops and marks itself truncated instead.
    """


class CollectionKind(StrEnum):
    """What the source body was; decides nothing at query time.

    The DOC kinds are single documents parked for reading and searching (the
    response_* tools): one record holding the whole payload. They live in the
    same tables under the same TTL and quotas — search and filtration are both
    database-level work, and both must survive a task boundary.
    """

    JSON = "json"
    CSV = "csv"
    DOC_JSON = "doc_json"
    DOC_TEXT = "doc_text"


class QueryOp(StrEnum):
    """The operations of the DSL."""

    GET = "get"
    PLUCK = "pluck"
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    DISTINCT = "distinct"


#: Operations that aggregate to one row (and may be grouped).
AGGREGATE_OPS = frozenset({QueryOp.COUNT, QueryOp.SUM, QueryOp.AVG, QueryOp.MIN, QueryOp.MAX})
#: Operations that require a field.
FIELD_OPS = frozenset(
    {QueryOp.PLUCK, QueryOp.SUM, QueryOp.AVG, QueryOp.MIN, QueryOp.MAX, QueryOp.DISTINCT}
)
#: Operations whose field must be numeric in the derived schema.
NUMERIC_OPS = frozenset({QueryOp.SUM, QueryOp.AVG})


class FilterOp(StrEnum):
    """Predicate operators; comparison semantics follow the field's type."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    CONTAINS = "contains"


@dataclass(frozen=True, slots=True)
class FilterPredicate:
    """One condition on a record field (dotted path into the payload)."""

    field: str
    op: FilterOp
    value: str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class JoinSpec:
    """An equality join against a second record set.

    `ref` may be another collection or the SAME one (then `source` usually
    tells the two record kinds apart). The join is the point of `into:` —
    records fetched from two endpoints meet in the database, not in the
    context window.
    """

    #: The right-hand collection (a bare id; the tool strips `col:`).
    ref: str
    #: Left record field that must equal…
    on_left: str
    #: …this right record field.
    on_right: str
    #: Restrict the right side to records of this source tag.
    source: str | None = None


#: Operations a join combines with (row-shaped plus the plain count).
JOIN_OPS = frozenset({"get", "count"})


@dataclass(frozen=True, slots=True)
class Query:
    """A validated query over one collection's records."""

    op: QueryOp
    #: Dotted path into the record for FIELD_OPS; None for get/count.
    field: str | None = None
    filters: tuple[FilterPredicate, ...] = ()
    #: Dotted path to group an aggregate by; only with AGGREGATE_OPS.
    group_by: str | None = None
    #: Restrict to records carrying this source tag (multi-endpoint collections).
    source: str | None = None
    #: Pair every (filtered) record with matches from a second set.
    join: JoinSpec | None = None
    limit: int = DEFAULT_QUERY_LIMIT
    offset: int = 0


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Rows as the engine returned them, plus how many matched in total."""

    rows: list[Any]
    total: int


@dataclass(frozen=True, slots=True)
class CollectionPassport:
    """Everything the model is told about a collection instead of its body."""

    id: str
    owner_id: str
    label: str
    kind: CollectionKind
    source: str
    #: The derived schema document (see schema_infer); a JSON value, not DDL.
    schema: dict[str, Any]
    #: Scalar siblings of the unwrapped items array ({"status": "ok"}).
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
    """Behavior knobs, mapped from the deployment's settings."""

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    max_per_user: int = DEFAULT_MAX_PER_USER
    max_bytes_per_user: int = DEFAULT_MAX_MB_PER_USER * 1024 * 1024
    query_default_limit: int = DEFAULT_QUERY_LIMIT
    query_max_limit: int = DEFAULT_QUERY_MAX_LIMIT
    #: Bodies at or under this stay inline in the tool result; only bigger
    #: ones become collections (absorbs the old per-tool truncation caps).
    inline_max_chars: int = DEFAULT_INLINE_MAX_CHARS
    #: The collect loop's page ceiling — what keeps one call from walking a
    #: gigabyte API to the end. A call may ask for fewer pages, never more.
    collect_max_pages: int = DEFAULT_COLLECT_MAX_PAGES
    #: What a collection_query answer takes when the caller does not choose,
    #: and the most it may deliberately ask for.
    query_default_chars: int = DEFAULT_QUERY_DEFAULT_CHARS
    query_max_chars: int = DEFAULT_QUERY_MAX_CHARS


@dataclass(frozen=True, slots=True)
class NewRecords:
    """One batch of records entering a collection."""

    #: JSON payloads, one per record, in order.
    payloads: list[dict[str, Any]]
    #: Source tag every record of the batch carries (endpoint name etc.).
    source: str = ""


class CollectionStore(Protocol):
    """Rows of the two fixed tables; owner scoping is a SQL predicate."""

    async def create(  # noqa: PLR0913, PLR0917 — a passport's worth of fields
        self,
        owner_id: str,
        label: str,
        kind: CollectionKind,
        source: str,
        schema: dict[str, Any],
        envelope: dict[str, Any],
        records: NewRecords,
        byte_size: int,
        truncated: bool,
        expires_at: datetime,
    ) -> CollectionPassport:
        """Create a collection with its first batch, evicting over-quota LRU."""
        ...

    async def append(  # noqa: PLR0913, PLR0917 — one batch's worth of facts
        self,
        owner_id: str,
        collection_id: str,
        records: NewRecords,
        schema: dict[str, Any],
        byte_size: int,
        expires_at: datetime,
    ) -> CollectionPassport:
        """Append a batch, replacing the derived schema and refreshing the TTL.

        Raises `CollectionNotFoundError` for a ref the owner does not hold.
        """
        ...

    async def passport(self, owner_id: str, collection_id: str) -> CollectionPassport:
        """The passport, or `CollectionNotFoundError` (expired ones included)."""
        ...

    async def mark_truncated(self, owner_id: str, collection_id: str) -> None:
        """Persist that the source was cut (page limit, wire limit) mid-fill."""
        ...

    async def single_payload(self, owner_id: str, collection_id: str) -> dict[str, Any]:
        """The first record's payload — how a parked document is read back.

        Raises `CollectionNotFoundError` (a document with no record counts
        as gone: there is nothing to read).
        """
        ...

    async def delete_expired(self) -> int:
        """Drop every collection past its TTL; returns how many went."""
        ...


class QueryEngine(Protocol):
    """Executes a validated `Query` where the data lies (the modular half).

    The v1 implementation compiles to Postgres jsonb SQL; anything else —
    an in-process evaluator, a different database — is a new implementation
    of this port, not a change to the tools.
    """

    async def execute(self, owner_id: str, collection_id: str, query: Query) -> QueryResult:
        """Run the query; raises `CollectionQueryError`/`CollectionNotFoundError`."""
        ...
