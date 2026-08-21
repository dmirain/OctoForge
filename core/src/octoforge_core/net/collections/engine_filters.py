"""Schema-aware WHERE predicate compilation."""

from octoforge_core.net.collections.api import CollectionQueryError, FilterOp, FilterPredicate
from octoforge_core.net.collections.engine_sql import Params
from octoforge_core.net.collections.engine_types import PredicateContext, WhereContext
from octoforge_core.net.collections.schema_infer import TYPE_NUMBER, field_node

COMPARISONS = {
    FilterOp.EQ: "=",
    FilterOp.NE: "<>",
    FilterOp.GT: ">",
    FilterOp.LT: "<",
    FilterOp.GTE: ">=",
    FilterOp.LTE: "<=",
}


def where(context: WhereContext, params: Params) -> str:
    """Select records from one collection, source, and filter set."""
    prefix = f"{context.alias}." if context.alias else ""
    compile_context = context.compile
    plan = compile_context.plan
    parts = [f"{prefix}collection_id = {params.value(compile_context.collection_id)}"]
    if plan.source is not None:
        parts.append(f"{prefix}source = {params.value(plan.source)}")
    parts.extend(
        predicate_sql(PredicateContext(predicate, compile_context.schema, prefix), params)
        for predicate in plan.filters
    )
    return " AND ".join(parts)


def predicate_sql(context: PredicateContext, params: Params) -> str:
    """Compile one validated predicate without putting input in SQL text."""
    predicate = context.predicate
    node = field_node(context.schema, predicate.field)
    numeric = node is not None and node.get("type") == TYPE_NUMBER
    if predicate.value is None:
        return _null_predicate(predicate, context.prefix, params)
    if predicate.op is FilterOp.CONTAINS:
        column = f"{context.prefix}payload #>> {params.path(predicate.field)}"
        return f"{column} ILIKE {params.value(f'%{predicate.value}%')}"
    comparison = COMPARISONS[predicate.op]
    if numeric and _numeric_value(predicate.value):
        column = f"({context.prefix}payload #>> {params.path(predicate.field)})::numeric"
        return f"{column} {comparison} {params.value(predicate.value)}"
    column = f"{context.prefix}payload #>> {params.path(predicate.field)}"
    rendered = (
        str(predicate.value).lower() if isinstance(predicate.value, bool) else str(predicate.value)
    )
    return f"{column} {comparison} {params.value(rendered)}"


def _null_predicate(predicate: FilterPredicate, prefix: str, params: Params) -> str:
    column = f"{prefix}payload #>> {params.path(predicate.field)}"
    if predicate.op is FilterOp.EQ:
        return f"{column} IS NULL"
    if predicate.op is FilterOp.NE:
        return f"{column} IS NOT NULL"
    raise CollectionQueryError(f"'{predicate.op.value}' cannot compare with null")


def _numeric_value(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
