"""Validate collection-tool boundary data into query objects."""

from typing import Any

from octoforge_core.net.collections.api import (
    REF_PREFIX,
    CollectionConfig,
    FilterOp,
    FilterPredicate,
    Query,
    QueryOp,
)
from octoforge_core.tools.errors import ToolArgumentsError


def _parse_ref(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("ref must be a non-empty string like 'col:…'")
    return raw.strip().removeprefix(REF_PREFIX)


def _parse_query(arguments: dict[str, Any], config: CollectionConfig) -> Query:
    try:
        op = QueryOp(str(arguments.get("op")))
    except ValueError as exc:
        raise ToolArgumentsError(f"unknown op: {arguments.get('op')!r}") from exc
    field = _optional_string(arguments.get("field"), "field")
    group_by = _optional_string(arguments.get("group_by"), "group_by")
    source = _optional_string(arguments.get("source"), "source")
    return Query(
        op=op,
        field=field,
        filters=_parse_filters(arguments.get("filters")),
        group_by=group_by,
        source=source,
        limit=_parse_int(arguments.get("limit"), config.query_default_limit, "limit"),
        offset=_parse_int(arguments.get("offset"), 0, "offset"),
    )


def _optional_string(raw: object, name: str) -> str | None:
    if raw is not None and not isinstance(raw, str):
        raise ToolArgumentsError(f"{name} must be a string")
    return raw


def _parse_filters(raw: object) -> tuple[FilterPredicate, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ToolArgumentsError("filters must be an array of {field, op, value}")
    return tuple(_parse_filter(item) for item in raw)


def _parse_filter(raw: object) -> FilterPredicate:
    if not isinstance(raw, dict) or not isinstance(raw.get("field"), str):
        raise ToolArgumentsError("each filter needs a string 'field'")
    try:
        op = FilterOp(str(raw.get("op")))
    except ValueError as exc:
        raise ToolArgumentsError(f"unknown filter op: {raw.get('op')!r}") from exc
    value = raw.get("value")
    if value is not None and not isinstance(value, str | int | float | bool):
        raise ToolArgumentsError("filter value must be a scalar or null")
    return FilterPredicate(field=raw["field"], op=op, value=value)


def _parse_int(raw: object, default: int, name: str) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolArgumentsError(f"{name} must be an integer")
    return raw
