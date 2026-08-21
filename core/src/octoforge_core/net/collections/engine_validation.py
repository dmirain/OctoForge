"""Schema-aware validation and paging normalization for collection queries."""

from dataclasses import dataclass, replace

from octoforge_core.net.collections.api import (
    AGGREGATE_OPS,
    FIELD_OPS,
    JOIN_OPS,
    NUMERIC_OPS,
    CollectionConfig,
    CollectionQueryError,
    Query,
    QueryOp,
)
from octoforge_core.net.collections.schema_infer import (
    TYPE_NUMBER,
    SchemaNode,
    field_node,
    known_fields,
)

NO_FIELD_TEMPLATE = "field '{field}' is not in this collection's schema; its records have: {known}"
NOT_NUMERIC_TEMPLATE = (
    "'{op}' needs a numeric field, but '{field}' is {kind} in this collection; "
    "numeric coercion is declared in the endpoint record's response section"
)


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Both schemas and configuration needed to validate one query."""

    query: Query
    schema: SchemaNode
    right_schema: SchemaNode | None
    config: CollectionConfig


def validate(context: ValidationContext) -> Query:
    """Check every named field and clamp page bounds."""
    query = context.query
    if query.op in FIELD_OPS:
        if query.field is None:
            raise CollectionQueryError(f"'{query.op.value}' needs a field")
        _require_field(context.schema, query.field, query.op)
    if query.group_by is not None:
        if query.op not in AGGREGATE_OPS:
            raise CollectionQueryError("group_by combines only with count/sum/avg/min/max")
        _require_field(context.schema, query.group_by)
    _validate_join(context)
    for predicate in query.filters:
        _require_field(context.schema, predicate.field)
    limit = min(max(query.limit, 1), context.config.query_max_limit)
    offset = max(query.offset, 0)
    if limit == query.limit and offset == query.offset:
        return query
    return replace(query, limit=limit, offset=offset)


def _validate_join(context: ValidationContext) -> None:
    join = context.query.join
    if join is None:
        return
    if context.query.op.value not in JOIN_OPS:
        allowed = ", ".join(sorted(JOIN_OPS))
        raise CollectionQueryError(f"join combines only with: {allowed}")
    assert context.right_schema is not None
    _require_field(context.schema, join.on_left)
    _require_field(context.right_schema, join.on_right)


def _require_field(schema: SchemaNode, path: str, numeric_for: QueryOp | None = None) -> None:
    node = field_node(schema, path)
    if node is None:
        known = ", ".join(known_fields(schema)) or "(no fields)"
        raise CollectionQueryError(NO_FIELD_TEMPLATE.format(field=path, known=known))
    if numeric_for in NUMERIC_OPS and node.get("type") != TYPE_NUMBER:
        operation = numeric_for.value if numeric_for else ""
        message = NOT_NUMERIC_TEMPLATE.format(op=operation, field=path, kind=node.get("type"))
        raise CollectionQueryError(message)
