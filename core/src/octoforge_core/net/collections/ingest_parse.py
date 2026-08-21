"""Structured-body sniffing and parsing."""

import asyncio
import json

from octoforge_core.net.collections.api import CollectionKind
from octoforge_core.net.collections.ingest_csv import parse_csv
from octoforge_core.net.collections.ingest_models import ParsedBody
from octoforge_core.net.collections.ingest_unwrap import MAX_RECORDS, as_records, scalars_of, unwrap

PARSE_IN_THREAD_CHARS = 64 * 1024
JSON_CONTENT_MARKERS = ("application/json", "text/json", "+json")
CSV_CONTENT_MARKERS = ("text/csv", "application/csv")


async def parse_structured(
    body: str, content_type: str, items_path: str | None = None
) -> ParsedBody | None:
    """Sniff and parse a body; answer None when its raw head is more honest."""
    declared = content_type.lower()
    if any(marker in declared for marker in CSV_CONTENT_MARKERS):
        return await asyncio.to_thread(parse_csv, body)
    if not _looks_json(body, declared):
        return None
    value = await _parse_json(body)
    return None if value is _INVALID else take_apart(value, items_path)


_INVALID = object()


async def _parse_json(body: str) -> object:
    try:
        if len(body) > PARSE_IN_THREAD_CHARS:
            return await asyncio.to_thread(json.loads, body)
        return json.loads(body)
    except ValueError:
        return _INVALID


def _looks_json(body: str, declared: str) -> bool:
    if any(marker in declared for marker in JSON_CONTENT_MARKERS):
        return True
    return body.lstrip()[:1] in ("{", "[")


def take_apart(value: object, items_path: str | None) -> ParsedBody | None:
    if items_path is not None:
        return _take_declared(value, items_path)
    records, envelope, dropped = unwrap(value)
    if records is None:
        return None
    single = isinstance(value, dict) and len(records.payloads) == 1
    single = single and records.payloads[0] is value
    return ParsedBody(CollectionKind.JSON, records, envelope, value, single, dropped)


def _take_declared(value: object, items_path: str) -> ParsedBody | None:
    found = dotted_get(value, items_path)
    if not isinstance(found, list):
        return None
    return ParsedBody(
        kind=CollectionKind.JSON,
        records=as_records(found),
        envelope=scalars_of(value),
        document=value,
        record_truncated=len(found) > MAX_RECORDS,
    )


def dotted_get(value: object, path: str) -> object:
    """Resolve a dotted path inside parsed JSON; None when a step misses."""
    node = value
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node
