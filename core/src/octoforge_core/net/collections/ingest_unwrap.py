"""Record discovery inside JSON response envelopes."""

from typing import Any

from octoforge_core.net.collections.api import NewRecords

MAX_RECORDS = 100_000
MAX_UNWRAP_DEPTH = 5
MAX_UNWRAP_NODES = 2000


def unwrap(value: object) -> tuple[NewRecords | None, dict[str, Any], bool]:
    """Find records in a parsed body and retain their scalar envelope."""
    if isinstance(value, list):
        return as_records(value), {}, len(value) > MAX_RECORDS
    if not isinstance(value, dict):
        return None, {}, False
    located = _locate_records(value)
    if located is None:
        return NewRecords(payloads=[value]), {}, False
    items, envelope = located
    return as_records(items), envelope, len(items) > MAX_RECORDS


def _locate_records(root: dict[str, Any]) -> tuple[list[object], dict[str, Any]] | None:
    root_scalars = scalars_of(root)
    frontier: list[dict[str, Any]] = [root]
    visited = 0
    for _ in range(MAX_UNWRAP_DEPTH):
        candidates, deeper, visited = _scan_level(frontier, visited)
        if candidates:
            items, parent = max(candidates, key=lambda pair: len(pair[0]))
            return items, {**root_scalars, **_envelope_of(parent, items)}
        if visited > MAX_UNWRAP_NODES or not deeper:
            return None
        frontier = deeper
    return None


def _scan_level(
    frontier: list[dict[str, Any]], visited: int
) -> tuple[list[tuple[list[object], dict[str, Any]]], list[dict[str, Any]], int]:
    candidates: list[tuple[list[object], dict[str, Any]]] = []
    deeper: list[dict[str, Any]] = []
    for node in frontier:
        visited += 1
        if visited > MAX_UNWRAP_NODES:
            break
        _classify_members(node, candidates, deeper)
    return candidates, deeper, visited


def _classify_members(
    node: dict[str, Any],
    candidates: list[tuple[list[object], dict[str, Any]]],
    deeper: list[dict[str, Any]],
) -> None:
    for member in node.values():
        if _is_object_array(member):
            candidates.append((member, node))
        elif isinstance(member, dict):
            deeper.append(member)


def _is_object_array(member: object) -> bool:
    return (
        isinstance(member, list) and bool(member) and all(isinstance(item, dict) for item in member)
    )


def _envelope_of(parent: dict[str, Any], records: list[object]) -> dict[str, Any]:
    return {
        name: member
        for name, member in parent.items()
        if member is not records and not isinstance(member, list)
    }


def scalars_of(value: object, except_key: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        name: member
        for name, member in value.items()
        if name != except_key and not isinstance(member, dict | list)
    }


def as_records(items: list[object]) -> NewRecords:
    payloads: list[dict[str, Any]] = []
    for item in items[:MAX_RECORDS]:
        payloads.append(item if isinstance(item, dict) else {"value": item})
    return NewRecords(payloads=payloads)
