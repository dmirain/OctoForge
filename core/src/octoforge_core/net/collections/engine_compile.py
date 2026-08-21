"""Main collection query statement compilation."""

from octoforge_core.net.collections.api import QueryOp
from octoforge_core.net.collections.engine_filters import where
from octoforge_core.net.collections.engine_join import compile_join
from octoforge_core.net.collections.engine_sql import Params, page
from octoforge_core.net.collections.engine_types import CompileContext, Compiled, WhereContext
from octoforge_core.net.collections.schema_infer import TYPE_NUMBER, field_node

AGGREGATES = {
    QueryOp.SUM: "sum",
    QueryOp.AVG: "avg",
    QueryOp.MIN: "min",
    QueryOp.MAX: "max",
}


def compile_query(context: CompileContext) -> Compiled:
    """Compile a validated query into SQL and bound parameters."""
    params = Params()
    if context.plan.join is not None:
        return compile_join(context, params)
    selection = where(WhereContext(context), params)
    sql = _main_sql(context, selection, params)
    return params.compiled(sql)


def _main_sql(context: CompileContext, selection: str, params: Params) -> str:
    plan = context.plan
    paging = (plan.limit, plan.offset)
    if plan.op is QueryOp.GET:
        return (
            f"SELECT payload FROM collection_records WHERE {selection} "
            f"ORDER BY position {page(paging, params)}"
        )
    if plan.op is QueryOp.PLUCK:
        assert plan.field is not None
        return (
            f"SELECT payload #> {params.path(plan.field)} FROM collection_records "
            f"WHERE {selection} ORDER BY position {page(paging, params)}"
        )
    if plan.op is QueryOp.DISTINCT:
        assert plan.field is not None
        return (
            f"SELECT DISTINCT payload #> {params.path(plan.field)} FROM collection_records "
            f"WHERE {selection} ORDER BY 1 {page(paging, params)}"
        )
    if plan.group_by is not None:
        group = f"payload #>> {params.path(plan.group_by)}"
        aggregate = aggregate_sql(context, params)
        return (
            f"SELECT {group} AS grp, {aggregate} FROM collection_records WHERE {selection} "
            f"GROUP BY 1 ORDER BY 2 DESC NULLS LAST {page(paging, params)}"
        )
    return f"SELECT {aggregate_sql(context, params)} FROM collection_records WHERE {selection}"


def aggregate_sql(context: CompileContext, params: Params) -> str:
    """Render an aggregate after validation has established its field."""
    plan = context.plan
    if plan.op is QueryOp.COUNT:
        return "count(*)"
    assert plan.field is not None
    column = f"payload #>> {params.path(plan.field)}"
    node = field_node(context.schema, plan.field)
    if node is not None and node.get("type") == TYPE_NUMBER:
        column = f"({column})::numeric"
    return f"{AGGREGATES[plan.op]}({column})"
