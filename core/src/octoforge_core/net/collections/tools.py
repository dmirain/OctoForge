"""The agent-facing tools over collections: query the data, re-read the passport."""

import json
from typing import Any

from octoforge_core.net.collections.api import (
    REF_PREFIX,
    CollectionConfig,
    CollectionError,
    CollectionNotFoundError,
    CollectionStore,
    FilterOp,
    FilterPredicate,
    Query,
    QueryEngine,
    QueryOp,
    QueryResult,
)
from octoforge_core.net.collections.ingest import render_passport
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec
from octoforge_core.tools.errors import ToolArgumentsError

QUERY_NAME = "collection_query"
QUERY_DESCRIPTION = (
    "Query a collection created from a large HTTP response (ref like 'col:…' from the "
    "call's passport). Runs in the database, not in context: use it instead of asking "
    "for the raw body. Ops: get (records), pluck (one field of every record), count, "
    "sum/avg/min/max (optionally per group_by), distinct. Filters narrow every op; "
    "field paths are dotted (owner.city) and must exist in the passport's schema."
)
GET_NAME = "collection_get"
GET_DESCRIPTION = (
    "Re-read the passport of a collection (ref like 'col:…'): schema, record count, "
    "expiry. Use it when the passport has fallen out of context; an expired or "
    "unknown ref answers not-found — fetch the data again in that case."
)
NOT_FOUND_TEMPLATE = (
    "collection '{ref}' is gone (expired, evicted or never yours): run the call that "
    "produced it again to get fresh data and a fresh ref"
)
#: The rendered result is capped; pages continue with offset.
MAX_RESULT_CHARS = 8000
RESULT_TEMPLATE = "{rows} of {total} matched (offset {offset})\n{body}"

QUERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Collection ref from the passport (col:…)"},
        "op": {
            "type": "string",
            "enum": [op.value for op in QueryOp],
            "description": "What to compute",
        },
        "field": {
            "type": "string",
            "description": "Dotted record field for pluck/sum/avg/min/max/distinct",
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "op": {"type": "string", "enum": [op.value for op in FilterOp]},
                    "value": {"type": ["string", "number", "boolean", "null"]},
                },
                "required": ["field", "op"],
            },
            "description": "Conditions every counted/returned record must satisfy",
        },
        "group_by": {
            "type": "string",
            "description": "Dotted field to group an aggregate by (count/sum/avg/min/max)",
        },
        "source": {
            "type": "string",
            "description": "Only records that arrived from this source tag",
        },
        "limit": {"type": "integer", "description": "Rows per page"},
        "offset": {"type": "integer", "description": "Rows to skip (paging)"},
    },
    "required": ["ref", "op"],
}
GET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Collection ref (col:…)"},
    },
    "required": ["ref"],
}


class CollectionQueryTool:
    """Executes the DSL over one collection through the engine port."""

    def __init__(self, engine: QueryEngine, config: CollectionConfig | None = None) -> None:
        self._engine = engine
        self._config = config or CollectionConfig()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=QUERY_NAME, description=QUERY_DESCRIPTION, parameters_schema=QUERY_SCHEMA
        )

    def visible_to(self, context: ToolContext) -> bool:
        """Collections exist only where HTTP calls do; same plan gate."""
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):  # defence in depth behind the spec filter
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = _parse_ref(arguments.get("ref"))
        query = _parse_query(arguments, self._config)
        try:
            result = await self._engine.execute(context.user_id, ref, query)
        except CollectionError as failure:
            return _failure_text(failure, arguments.get("ref"))
        return _render_result(query, result)


class CollectionGetTool:
    """Re-reads a passport (the schema reminder after compaction)."""

    def __init__(self, store: CollectionStore) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=GET_NAME, description=GET_DESCRIPTION, parameters_schema=GET_SCHEMA)

    def visible_to(self, context: ToolContext) -> bool:
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):  # defence in depth behind the spec filter
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = _parse_ref(arguments.get("ref"))
        try:
            passport = await self._store.passport(context.user_id, ref)
        except CollectionError as failure:
            return _failure_text(failure, arguments.get("ref"))
        return render_passport(passport)


def _parse_ref(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolArgumentsError("ref must be a non-empty string like 'col:…'")
    return raw.strip().removeprefix(REF_PREFIX)


def _parse_query(arguments: dict[str, Any], config: CollectionConfig) -> Query:
    try:
        op = QueryOp(str(arguments.get("op")))
    except ValueError as exc:
        raise ToolArgumentsError(f"unknown op: {arguments.get('op')!r}") from exc
    field = arguments.get("field")
    if field is not None and not isinstance(field, str):
        raise ToolArgumentsError("field must be a string")
    group_by = arguments.get("group_by")
    if group_by is not None and not isinstance(group_by, str):
        raise ToolArgumentsError("group_by must be a string")
    source = arguments.get("source")
    if source is not None and not isinstance(source, str):
        raise ToolArgumentsError("source must be a string")
    return Query(
        op=op,
        field=field,
        filters=_parse_filters(arguments.get("filters")),
        group_by=group_by,
        source=source,
        limit=_parse_int(arguments.get("limit"), config.query_default_limit, "limit"),
        offset=_parse_int(arguments.get("offset"), 0, "offset"),
    )


def _parse_filters(raw: object) -> tuple[FilterPredicate, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ToolArgumentsError("filters must be an array of {field, op, value}")
    predicates: list[FilterPredicate] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("field"), str):
            raise ToolArgumentsError("each filter needs a string 'field'")
        try:
            op = FilterOp(str(item.get("op")))
        except ValueError as exc:
            raise ToolArgumentsError(f"unknown filter op: {item.get('op')!r}") from exc
        value = item.get("value")
        if value is not None and not isinstance(value, str | int | float | bool):
            raise ToolArgumentsError("filter value must be a scalar or null")
        predicates.append(FilterPredicate(field=item["field"], op=op, value=value))
    return tuple(predicates)


def _parse_int(raw: object, default: int, name: str) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolArgumentsError(f"{name} must be an integer")
    return raw


def _failure_text(failure: CollectionError, ref: object) -> str:
    if isinstance(failure, CollectionNotFoundError):
        return NOT_FOUND_TEMPLATE.format(ref=ref)
    return f"query refused: {failure}"


def _render_result(query: Query, result: QueryResult) -> str:
    body = json.dumps(result.rows, ensure_ascii=False, default=str)
    if len(body) > MAX_RESULT_CHARS:
        body = body[:MAX_RESULT_CHARS] + "\n…[cut: narrow the query or page with offset/limit]"
    return RESULT_TEMPLATE.format(
        rows=len(result.rows), total=result.total, offset=query.offset, body=body
    )
