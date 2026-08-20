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
    JoinSpec,
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
        "join": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Right-hand collection (col:…)"},
                "on_left": {"type": "string", "description": "Left record field"},
                "on_right": {"type": "string", "description": "Right record field it must equal"},
                "source": {
                    "type": "string",
                    "description": "Only right records of this source tag",
                },
            },
            "required": ["ref", "on_left", "on_right"],
            "description": (
                "Pair every record with its matches from another collection (or the "
                "same one, told apart by source) on field equality — the join runs "
                "in the database. Combines with get (pairs) and count"
            ),
        },
        "limit": {"type": "integer", "description": "Rows per page"},
        "offset": {"type": "integer", "description": "Rows to skip (paging)"},
        "max_chars": {
            "type": "integer",
            "description": (
                "How much of the rendered result you choose to take (the default "
                "is conservative; raise it deliberately for big reads)"
            ),
        },
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
        asked = _parse_int(
            arguments.get("max_chars"), self._config.query_default_chars, "max_chars"
        )
        try:
            result = await self._engine.execute(context.user_id, ref, query)
        except CollectionError as failure:
            return _failure_text(failure, arguments.get("ref"))
        return _render_result(query, result, min(asked, self._config.query_max_chars))


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
        join=_parse_join(arguments.get("join")),
        limit=_parse_int(arguments.get("limit"), config.query_default_limit, "limit"),
        offset=_parse_int(arguments.get("offset"), 0, "offset"),
    )


def _parse_join(raw: object) -> JoinSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ToolArgumentsError("join must be an object: {ref, on_left, on_right, source?}")
    ref = raw.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise ToolArgumentsError("join.ref must be a collection ref like 'col:…'")
    on_left, on_right = raw.get("on_left"), raw.get("on_right")
    if not isinstance(on_left, str) or not isinstance(on_right, str):
        raise ToolArgumentsError("join.on_left and join.on_right must be field paths")
    source = raw.get("source")
    if source is not None and not isinstance(source, str):
        raise ToolArgumentsError("join.source must be a string")
    return JoinSpec(
        ref=ref.strip().removeprefix(REF_PREFIX),
        on_left=on_left,
        on_right=on_right,
        source=source,
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


def _render_result(query: Query, result: QueryResult, max_chars: int) -> str:
    body = json.dumps(result.rows, ensure_ascii=False, default=str)
    if len(body) > max_chars:
        body = body[:max_chars] + (
            "\n…[cut: narrow the query, page with offset/limit, or raise max_chars]"
        )
    return RESULT_TEMPLATE.format(
        rows=len(result.rows), total=result.total, offset=query.offset, body=body
    )
