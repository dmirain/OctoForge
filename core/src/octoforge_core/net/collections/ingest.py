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
from datetime import datetime, timedelta
from typing import Any

from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionKind,
    CollectionPassport,
    CollectionStore,
    NewRecords,
)
from octoforge_core.net.collections.schema_infer import infer_records, render
from octoforge_core.time import utc_now

logger = logging.getLogger(__name__)

#: Bodies longer than this are parsed in a worker thread.
PARSE_IN_THREAD_CHARS = 64 * 1024
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
    """Turns an oversized structured body into a collection + passport."""

    def __init__(self, store: CollectionStore, config: CollectionConfig) -> None:
        self._store = store
        self._config = config

    @property
    def inline_max_chars(self) -> int:
        return self._config.inline_max_chars

    async def spill(  # noqa: PLR0913, PLR0917 — the call-site boundary
        self,
        owner_id: str,
        body: str,
        content_type: str,
        source: str,
        wire_truncated: bool,
        label: str = "",
    ) -> str | None:
        """The passport text, or None when the body is small or not structured.

        Never raises: a spill failure must not fail the call that produced
        the data — the caller's truncation path is always there to fall back
        to, and the failure is logged instead.
        """
        if len(body) <= self._config.inline_max_chars:
            return None
        try:
            parsed = await _parse(body, content_type)
            if parsed is None:
                return None
            kind, records, envelope = parsed
            passport = await self._store.create(
                owner_id=owner_id,
                label=label,
                kind=kind,
                source=source,
                schema=infer_records(records.payloads),
                envelope=envelope,
                records=records,
                byte_size=len(body),
                truncated=wire_truncated,
                expires_at=self._expiry(),
            )
        except Exception:
            logger.exception("response spill failed; falling back to truncation")
            return None
        return render_passport(passport)

    def _expiry(self) -> datetime:
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


async def _parse(
    body: str, content_type: str
) -> tuple[CollectionKind, NewRecords, dict[str, Any]] | None:
    """Sniff and parse; None means "keep the old truncation"."""
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
    records, envelope = _unwrap(value)
    if records is None:
        return None
    return CollectionKind.JSON, records, envelope


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
            envelope = {
                name: member
                for name, member in value.items()
                if name != key and not isinstance(member, dict | list)
            }
            return _as_records(value[key]), envelope
        return NewRecords(payloads=[value]), {}
    return None, {}


def _as_records(items: list[Any]) -> NewRecords:
    payloads: list[dict[str, Any]] = []
    for item in items[:MAX_RECORDS]:
        payloads.append(item if isinstance(item, dict) else {"value": item})
    return NewRecords(payloads=payloads)


def _parse_csv(body: str) -> tuple[CollectionKind, NewRecords, dict[str, Any]] | None:
    """Header-first CSV into records; values stay strings (coercion is phase 2)."""
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
    return CollectionKind.CSV, NewRecords(payloads=payloads), {}
