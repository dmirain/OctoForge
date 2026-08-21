"""Compilation of cross-collection inner joins."""

from octoforge_core.net.collections.api import QueryOp
from octoforge_core.net.collections.engine_filters import where
from octoforge_core.net.collections.engine_sql import Params, page
from octoforge_core.net.collections.engine_types import CompileContext, Compiled, WhereContext


def join_clause(context: CompileContext, params: Params) -> str:
    """Render the common JOIN ON clause used by data and count statements."""
    join = context.plan.join
    assert join is not None
    condition = (
        f"b.collection_id = {params.value(join.ref)} "
        f"AND b.payload #>> {params.path(join.on_right)} = "
        f"a.payload #>> {params.path(join.on_left)}"
    )
    if join.source is not None:
        condition += f" AND b.source = {params.value(join.source)}"
    return f"JOIN collection_records b ON {condition}"


def compile_join(context: CompileContext, params: Params) -> Compiled:
    """Compile pairs of left and right records matched by field equality."""
    join = join_clause(context, params)
    selection = where(WhereContext(context, "a"), params)
    if context.plan.op is QueryOp.COUNT:
        sql = f"SELECT count(*) FROM collection_records a {join} WHERE {selection}"
    else:
        paging = page((context.plan.limit, context.plan.offset), params)
        sql = (
            f"SELECT a.payload, b.payload FROM collection_records a {join} "
            f"WHERE {selection} ORDER BY a.position, b.position {paging}"
        )
    return params.compiled(sql)
