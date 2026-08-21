"""Unpaged result counting for collection queries."""

from octoforge_core.net.collections.api import QueryOp
from octoforge_core.net.collections.engine_filters import where
from octoforge_core.net.collections.engine_join import join_clause
from octoforge_core.net.collections.engine_sql import Params
from octoforge_core.net.collections.engine_types import CompileContext, Compiled, WhereContext


def compile_count(context: CompileContext) -> Compiled:
    """Count everything the main statement would return without paging."""
    params = Params()
    if context.plan.join is not None:
        join = join_clause(context, params)
        selection = where(WhereContext(context, "a"), params)
        sql = f"SELECT count(*) FROM collection_records a {join} WHERE {selection}"
        return params.compiled(sql)
    selection = where(WhereContext(context), params)
    counted = _counted_expression(context, params)
    return params.compiled(f"SELECT {counted} FROM collection_records WHERE {selection}")


def _counted_expression(context: CompileContext, params: Params) -> str:
    plan = context.plan
    if plan.op is QueryOp.DISTINCT:
        assert plan.field is not None
        return f"count(DISTINCT payload #> {params.path(plan.field)})"
    if plan.group_by is not None:
        return f"count(DISTINCT payload #>> {params.path(plan.group_by)})"
    return "count(*)"
