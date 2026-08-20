"""Where a large structured response becomes a collection.

`ResponseSpill` is the façade the three call sites share (`external_call`,
`http_request`, MCP mirrors): given an already-scrubbed body it answers either
the passport text the model should see instead, or None — "not mine", and the
caller keeps its old truncation. Small bodies are None on purpose: below the
inline threshold the body itself is the best possible answer.

Parsing runs off the event loop for anything sizable: a 2 MB `json.loads` is
tens of milliseconds, which one dialog may spend but every other dialog must
not notice (the no-stop-the-world rule).
"""

import asyncio
import csv
import io
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionKind,
    CollectionPassport,
    CollectionStore,
    NewRecords,
)
from octoforge_core.net.collections.schema_infer import infer_records, merge_nodes, render
from octoforge_core.net.response_memory import ResponseMemory, render_memory_passport
from octoforge_core.net.tool_spec import FieldCoercion
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

#: Bodies longer than this are parsed in a worker thread.
PARSE_IN_THREAD_CHARS = 64 * 1024
#: The wire ceiling without a memory tier (the database tier's own cap).
DB_WIRE_LIMIT_BYTES = 2 * 1024 * 1024
#: A record-count backstop against pathological inputs (2 MB of `[1,1,1,…]`).
MAX_RECORDS = 100_000
#: The envelope is a courtesy, not a second body.
MAX_ENVELOPE_CHARS = 1000

JSON_CONTENT_MARKERS = ("application/json", "text/json", "+json")
CSV_CONTENT_MARKERS = ("text/csv", "application/csv")

PASSPORT_TEMPLATE = (
    "[collection {ref}] kind={kind} · source={source} · {records} records · "
    "{size} · expires in {minutes} min{truncated}\n"
    "record schema: {schema}\n"
    "{envelope}"
    "The body is NOT in this message — query the data with collection_query: "
    "ops get/pluck/count/sum/avg/min/max/distinct, filters, group_by; "
    "collection_get re-reads this passport."
)
TRUNCATED_NOTE = " · SOURCE CUT at the wire limit: counts reflect what arrived"


class ResponseSpill:
    """Routes an oversized body to where its shape belongs.

    Two tiers with different fates: an ARRAY of records goes to the database
    (its value is queries, and it must survive task boundaries), while a
    SINGLE document — one JSON object, an article, a page of anything — goes
    to task memory (its value is being read or searched, and it dies with
    the task). Unstructured text takes the memory tier verbatim, which is
    what finally makes a fetched HTML page or markdown file workable.

    Either tier may be absent: without Postgres there is no database tier
    (arrays fall back to memory as one readable document), and without a
    memory a document keeps the old truncation. The caller never learns any
    of this from an exception — the truncation path is always the fallback.
    """

    def __init__(
        self,
        store: CollectionStore | None,
        config: CollectionConfig,
        memory: ResponseMemory | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._memory = memory

    @property
    def inline_max_chars(self) -> int:
        return self._config.inline_max_chars

    @property
    def config(self) -> CollectionConfig:
        return self._config

    @property
    def store(self) -> CollectionStore | None:
        return self._store

    @property
    def wire_limit_bytes(self) -> int:
        """How much one response may pull off the wire before the cut.

        The memory tier raises it: RAM is the budget there, not context, and
        scenario three of this design is literally "the API answers 3 MB".
        """
        if self._memory is not None:
            return self._memory.config.max_response_chars
        return DB_WIRE_LIMIT_BYTES

    async def spill(  # noqa: PLR0913, PLR0917 — the call-site boundary
        self,
        owner_id: str,
        body: str,
        content_type: str,
        source: str,
        wire_truncated: bool,
        label: str = "",
        scope: str = "",
    ) -> str | None:
        """The passport text, or None for "keep the body inline/truncated".

        Never raises: a spill failure must not fail the call that produced
        the data — the caller's truncation path is always there to fall back
        to, and the failure is logged instead.
        """
        if len(body) <= self._config.inline_max_chars:
            return None
        try:
            return await self._route(
                owner_id, body, content_type, source, wire_truncated, label, scope
            )
        except Exception:
            logger.exception("response spill failed; falling back to truncation")
            return None

    async def _route(  # noqa: PLR0913, PLR0917 — the spill's own arguments
        self,
        owner_id: str,
        body: str,
        content_type: str,
        source: str,
        wire_truncated: bool,
        label: str,
        scope: str,
    ) -> str | None:
        parsed = await parse_structured(body, content_type)
        if parsed is None:
            # unstructured (text, HTML, broken JSON): memory holds it verbatim
            return self._remember(owner_id, scope, "text", source, body)
        if not parsed.records.payloads:
            return None
        if parsed.single_document:
            remembered = self._remember(
                owner_id, scope, "json", source, body, document=parsed.document
            )
            if remembered is not None:
                return remembered
            # no memory wired: a single document still beats truncation as
            # a one-record collection, when the database tier exists
        if self._store is None:
            # no database tier: an array is still a readable document
            return self._remember(owner_id, scope, "json", source, body, document=parsed.document)
        passport = await self._store.create(
            owner_id=owner_id,
            label=label,
            kind=parsed.kind,
            source=source,
            schema=infer_records(parsed.records.payloads),
            envelope=parsed.envelope,
            records=parsed.records,
            byte_size=len(body),
            truncated=wire_truncated,
            expires_at=self.expiry(),
        )
        return render_passport(passport)

    def _remember(  # noqa: PLR0913, PLR0917 — mirrors the memory's identity
        self,
        owner_id: str,
        scope: str,
        kind: str,
        source: str,
        body: str,
        document: Any = None,  # noqa: ANN401 — parsed JSON
    ) -> str | None:
        if self._memory is None:
            return None
        item = self._memory.store(
            owner_id=owner_id, scope=scope, kind=kind, source=source, body=body, document=document
        )
        return render_memory_passport(item, self._memory.config)

    def expiry(self) -> datetime:
        return utc_now() + timedelta(seconds=self._config.ttl_seconds)


def render_passport(passport: CollectionPassport) -> str:
    """The model-facing form of a collection's passport."""
    minutes = max(0, int((passport.expires_at - utc_now()).total_seconds() // 60))
    envelope = ""
    if passport.envelope:
        rendered = json.dumps(passport.envelope, ensure_ascii=False)
        if len(rendered) > MAX_ENVELOPE_CHARS:
            rendered = rendered[:MAX_ENVELOPE_CHARS] + "…"
        envelope = f"envelope: {rendered}\n"
    return PASSPORT_TEMPLATE.format(
        ref=passport.ref,
        kind=passport.kind.value,
        source=passport.source or "-",
        records=passport.record_count,
        size=_human_size(passport.byte_size),
        minutes=minutes,
        truncated=TRUNCATED_NOTE if passport.truncated else "",
        schema=render(passport.schema),
        envelope=envelope,
    )


KILOBYTE = 1024
MEGABYTE = KILOBYTE * KILOBYTE


def _human_size(chars: int) -> str:
    if chars >= MEGABYTE:
        return f"{chars / MEGABYTE:.1f} MB"
    if chars >= KILOBYTE:
        return f"{chars / KILOBYTE:.1f} KB"
    return f"{chars} chars"


@dataclass(frozen=True, slots=True)
class ParsedBody:
    """A structured body taken apart: the records, and what wrapped them.

    `document` is the whole parsed JSON (None for CSV) — the collect loop
    reads pagination cursors and totals off it by dotted path.
    """

    kind: CollectionKind
    records: NewRecords
    envelope: dict[str, Any]
    document: Any = None
    #: True when the body was one JSON object taken whole as one record —
    #: the shape whose home is task memory, not the database.
    single_document: bool = False


async def parse_structured(
    body: str, content_type: str, items_path: str | None = None
) -> ParsedBody | None:
    """Sniff and parse; None means "keep the old truncation".

    An explicit `items_path` (the record's `response.items_path`) overrides
    the unwrap heuristic — the author knows where the records live better
    than any guess about single-array envelopes.
    """
    declared = content_type.lower()
    if any(marker in declared for marker in CSV_CONTENT_MARKERS):
        return await asyncio.to_thread(_parse_csv, body)
    looks_json = any(marker in declared for marker in JSON_CONTENT_MARKERS)
    if not looks_json:
        head = body.lstrip()[:1]
        looks_json = head in ("{", "[")
    if not looks_json:
        return None
    try:
        if len(body) > PARSE_IN_THREAD_CHARS:
            value = await asyncio.to_thread(json.loads, body)
        else:
            value = json.loads(body)
    except ValueError:
        return None  # declared JSON that does not parse: the head is more honest
    return _take_apart(value, items_path)


def _take_apart(value: Any, items_path: str | None) -> ParsedBody | None:  # noqa: ANN401
    if items_path is not None:
        found = dotted_get(value, items_path)
        if not isinstance(found, list):
            return None  # the declared path missed; the raw head is more honest
        return ParsedBody(
            kind=CollectionKind.JSON,
            records=_as_records(found),
            envelope=_scalars_of(value),
            document=value,
        )
    records, envelope = _unwrap(value)
    if records is None:
        return None
    return ParsedBody(kind=CollectionKind.JSON, records=records, envelope=envelope, document=value)


def dotted_get(value: Any, path: str) -> Any:  # noqa: ANN401 — parsed JSON in and out
    """Resolve a dotted path inside parsed JSON; None when any step misses."""
    node = value
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def shape_records(records: NewRecords, fields: Mapping[str, FieldCoercion]) -> NewRecords:
    """Apply a record's declared projection and coercions.

    With fields declared, only they survive (declared-but-absent ones are
    simply absent); a value that refuses its coercion stays as it came, and
    the derived schema then tells the truth about it.
    """
    if not fields:
        return records
    shaped = [
        {name: _coerce(payload[name], kind) for name, kind in fields.items() if name in payload}
        for payload in records.payloads
    ]
    return NewRecords(payloads=shaped, source=records.source)


def _coerce(value: Any, kind: FieldCoercion) -> Any:  # noqa: ANN401 — parsed JSON in and out
    if kind is FieldCoercion.NUMBER and isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    if kind is FieldCoercion.STRING and value is not None and not isinstance(value, str):
        structured = isinstance(value, dict | list)
        return json.dumps(value, ensure_ascii=False) if structured else str(value)
    if kind is FieldCoercion.BOOLEAN and isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return value


class CollectionSink:
    """Accumulates batches into one collection, created on the first batch.

    The collect loop and the `into:` path both pour through this: it carries
    the merged schema across batches (the store replaces, so somebody must
    remember) and hides create-vs-append from the caller. `into` starts from
    the existing collection's schema, which is what makes records from a
    DIFFERENT endpoint mergeable — the join happens in the database later.
    """

    def __init__(
        self,
        spill: ResponseSpill,
        owner_id: str,
        source: str,
        label: str = "",
        into: str | None = None,
    ) -> None:
        self._spill = spill
        self._owner_id = owner_id
        self._source = source
        self._label = label
        self._collection_id = into
        self._started_from_existing = into is not None
        self._schema: dict[str, Any] | None = None
        self._kind = CollectionKind.JSON
        self.record_count = 0

    async def add(self, parsed: ParsedBody, byte_size: int) -> None:
        """Store one batch under this sink's source tag."""
        batch = NewRecords(payloads=parsed.records.payloads, source=self._source)
        store = self._spill.store
        assert store is not None  # the collect/into paths refuse before building a sink
        if self._schema is None and self._started_from_existing:
            assert self._collection_id is not None
            existing = await store.passport(self._owner_id, self._collection_id)
            self._schema = existing.schema
        merged = merge_nodes(self._schema, infer_records(batch.payloads))
        if self._collection_id is None:
            passport = await store.create(
                owner_id=self._owner_id,
                label=self._label,
                kind=parsed.kind,
                source=self._source,
                schema=merged,
                envelope=parsed.envelope,
                records=batch,
                byte_size=byte_size,
                truncated=False,
                expires_at=self._spill.expiry(),
            )
            self._collection_id = passport.id
        else:
            await store.append(
                self._owner_id,
                self._collection_id,
                batch,
                schema=merged,
                byte_size=byte_size,
                expires_at=self._spill.expiry(),
            )
        self._schema = merged
        self.record_count += len(batch.payloads)

    async def finish(self, truncated: bool) -> CollectionPassport | None:
        """The final passport (None when nothing was ever added).

        `truncated` is persisted: a later `collection_get` must still say the
        counts reflect what arrived, not what the API holds.
        """
        if self._collection_id is None:
            return None
        store = self._spill.store
        assert store is not None  # add() above required it
        if truncated:
            await store.mark_truncated(self._owner_id, self._collection_id)
        return await store.passport(self._owner_id, self._collection_id)


def _unwrap(value: Any) -> tuple[NewRecords | None, dict[str, Any]]:  # noqa: ANN401 — parsed JSON
    """Find the records inside a parsed body.

    A top-level array IS the records. An object with exactly one array-of-
    objects member is an envelope around its records — the scalar siblings
    ride into the passport. Anything else is a single record; a scalar body
    is nobody's collection.
    """
    if isinstance(value, list):
        return _as_records(value), {}
    if isinstance(value, dict):
        arrays = [
            key
            for key, member in value.items()
            if isinstance(member, list) and member and all(isinstance(x, dict) for x in member)
        ]
        if len(arrays) == 1:
            key = arrays[0]
            return _as_records(value[key]), _scalars_of(value, except_key=key)
        return NewRecords(payloads=[value]), {}
    return None, {}


def _scalars_of(value: Any, except_key: str | None = None) -> dict[str, Any]:  # noqa: ANN401
    if not isinstance(value, dict):
        return {}
    return {
        name: member
        for name, member in value.items()
        if name != except_key and not isinstance(member, dict | list)
    }


def _as_records(items: list[Any]) -> NewRecords:
    payloads: list[dict[str, Any]] = []
    for item in items[:MAX_RECORDS]:
        payloads.append(item if isinstance(item, dict) else {"value": item})
    return NewRecords(payloads=payloads)


def _parse_csv(body: str) -> ParsedBody | None:
    """Header-first CSV into records; values stay strings until coercions apply."""
    sample = body[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(body), dialect)
    rows = list(reader)
    if len(rows) < 2:  # noqa: PLR2004 — a header alone is not data
        return None
    header = [name.strip() or f"column_{i}" for i, name in enumerate(rows[0])]
    payloads = [
        {header[i]: cell for i, cell in enumerate(row) if i < len(header)}
        for row in rows[1 : MAX_RECORDS + 1]
    ]
    return ParsedBody(kind=CollectionKind.CSV, records=NewRecords(payloads=payloads), envelope={})
