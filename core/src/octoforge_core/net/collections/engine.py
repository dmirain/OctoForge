"""The Postgres query engine: compiles the DSL into SQL over jsonb.

Injection is excluded by construction, not by escaping: no caller-provided
string ever reaches the SQL text. Field paths travel as bound `text[]`
parameters into the `#>>` operator, values as ordinary bound parameters, and
everything else in the statement is chosen from fixed vocabularies (operators,
casts) after validation against the collection's derived schema.

The schema also picks the comparison semantics: a `number` field is cast to
numeric, everything else compares as text — which quietly does the right
thing for ISO dates, where lexical and chronological order agree.
"""

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session
from octoforge_core.net.collections.api import (
    AGGREGATE_OPS,
    FIELD_OPS,
    NUMERIC_OPS,
    CollectionConfig,
    CollectionNotFoundError,
    CollectionQueryError,
    FilterOp,
    FilterPredicate,
    Query,
    QueryOp,
    QueryResult,
)
from octoforge_core.net.collections.schema_infer import (
    TYPE_NUMBER,
    field_node,
    known_fields,
)
from octoforge_core.time import utc_now

#: FilterOp -> SQL comparison; CONTAINS is handled apart (ILIKE).
COMPARISONS = {
    FilterOp.EQ: "=",
    FilterOp.NE: "<>",
    FilterOp.GT: ">",
    FilterOp.LT: "<",
    FilterOp.GTE: ">=",
    FilterOp.LTE: "<=",
}
#: QueryOp -> SQL aggregate over the (already cast) field expression.
AGGREGATES = {
    QueryOp.SUM: "sum",
    QueryOp.AVG: "avg",
    QueryOp.MIN: "min",
    QueryOp.MAX: "max",
}

NO_FIELD_TEMPLATE = "field '{field}' is not in this collection's schema; its records have: {known}"
NOT_NUMERIC_TEMPLATE = (
    "'{op}' needs a numeric field, but '{field}' is {kind} in this collection; "
    "numeric coercion is declared in the endpoint record's response section"
)


@dataclass(frozen=True, slots=True)
class _Compiled:
    """One statement with its parameters, ready to execute."""

    sql: str
    params: dict[str, Any]
    array_params: tuple[str, ...]


class PostgresCollectionQueryEngine:
    """`QueryEngine` over the two fixed tables, Postgres only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: CollectionConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or CollectionConfig()

    async def execute(self, owner_id: str, collection_id: str, query: Query) -> QueryResult:
        """Validate against the derived schema, compile, run."""
        async with read_session(self._session_factory) as session:
            schema = await self._record_schema(session, owner_id, collection_id)
            plan = _validate(query, schema, self._config)
            main = _compile(plan, schema, collection_id)
            rows = [tuple(row) for row in (await session.execute(_statement(main))).all()]
            total = await self._total(session, plan, schema, collection_id, len(rows))
        return QueryResult(rows=_shape_rows(plan, rows), total=total)

    async def _record_schema(
        self, session: AsyncSession, owner_id: str, collection_id: str
    ) -> dict[str, Any]:
        """The collection's record schema; not-found covers expiry and others'."""
        found = (
            await session.execute(
                text(
                    "SELECT owner_id, expires_at, schema FROM collections WHERE id = :cid"
                ).bindparams(cid=collection_id)
            )
        ).first()
        if found is None or found[0] != owner_id or found[1] <= utc_now():
            raise CollectionNotFoundError(collection_id)
        raw = found[2]
        return dict(raw) if isinstance(raw, dict) else dict(json.loads(raw))

    async def _total(
        self,
        session: AsyncSession,
        plan: Query,
        schema: dict[str, Any],
        collection_id: str,
        returned: int,
    ) -> int:
        """How many rows matched regardless of limit/offset.

        Aggregates answer one row by definition; the extra COUNT runs only
        for row-shaped results, and only when the page might not be the whole
        answer.
        """
        if plan.op in AGGREGATE_OPS and plan.group_by is None:
            return 1
        if plan.offset == 0 and returned < plan.limit:
            return returned
        counter = _compile_count(plan, schema, collection_id)
        counted = (await session.execute(_statement(counter))).scalar()
        return int(counted or 0)


def _statement(compiled: _Compiled) -> Any:  # noqa: ANN401 — SQLAlchemy's executable
    statement = text(compiled.sql)
    binds = [
        bindparam(name, value=compiled.params[name], type_=ARRAY(Text()))
        if name in compiled.array_params
        else bindparam(name, value=compiled.params[name])
        for name in compiled.params
    ]
    return statement.bindparams(*binds)


def _validate(query: Query, schema: dict[str, Any], config: CollectionConfig) -> Query:
    """Check every named field against the schema; clamp the page size."""
    if query.op in FIELD_OPS:
        if query.field is None:
            raise CollectionQueryError(f"'{query.op.value}' needs a field")
        _require_field(schema, query.field, numeric_for=query.op)
    if query.group_by is not None:
        if query.op not in AGGREGATE_OPS:
            raise CollectionQueryError("group_by combines only with count/sum/avg/min/max")
        _require_field(schema, query.group_by)
    for predicate in query.filters:
        _require_field(schema, predicate.field)
    limit = min(max(query.limit, 1), config.query_max_limit)
    offset = max(query.offset, 0)
    if limit == query.limit and offset == query.offset:
        return query
    return Query(
        op=query.op,
        field=query.field,
        filters=query.filters,
        group_by=query.group_by,
        source=query.source,
        limit=limit,
        offset=offset,
    )


def _require_field(schema: dict[str, Any], path: str, numeric_for: QueryOp | None = None) -> None:
    node = field_node(schema, path)
    if node is None:
        known = ", ".join(known_fields(schema)) or "(no fields)"
        raise CollectionQueryError(NO_FIELD_TEMPLATE.format(field=path, known=known))
    if numeric_for in NUMERIC_OPS and node.get("type") != TYPE_NUMBER:
        raise CollectionQueryError(
            NOT_NUMERIC_TEMPLATE.format(
                op=numeric_for.value if numeric_for else "", field=path, kind=node.get("type")
            )
        )


class _Params:
    """Accumulates bound parameters, remembering which carry text[] paths."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.arrays: list[str] = []

    def path(self, dotted: str) -> str:
        name = f"p{len(self.values)}"
        self.values[name] = dotted.split(".")
        self.arrays.append(name)
        return f":{name}"

    def value(self, value: object) -> str:
        name = f"p{len(self.values)}"
        self.values[name] = value
        return f":{name}"


def _is_numeric(schema: dict[str, Any], dotted: str) -> bool:
    node = field_node(schema, dotted)
    return node is not None and node.get("type") == TYPE_NUMBER


def _where(plan: Query, collection_id: str, params: _Params, schema: dict[str, Any]) -> str:
    parts = [f"collection_id = {params.value(collection_id)}"]
    if plan.source is not None:
        parts.append(f"source = {params.value(plan.source)}")
    for predicate in plan.filters:
        parts.append(_predicate_sql(predicate, params, schema))
    return " AND ".join(parts)


def _predicate_sql(predicate: FilterPredicate, params: _Params, schema: dict[str, Any]) -> str:
    node = field_node(schema, predicate.field)
    numeric = node is not None and node.get("type") == TYPE_NUMBER
    if predicate.value is None:
        column = f"payload #>> {params.path(predicate.field)}"
        if predicate.op is FilterOp.EQ:
            return f"{column} IS NULL"
        if predicate.op is FilterOp.NE:
            return f"{column} IS NOT NULL"
        raise CollectionQueryError(f"'{predicate.op.value}' cannot compare with null")
    if predicate.op is FilterOp.CONTAINS:
        column = f"payload #>> {params.path(predicate.field)}"
        return f"{column} ILIKE {params.value(f'%{predicate.value}%')}"
    comparison = COMPARISONS[predicate.op]
    if (
        numeric
        and isinstance(predicate.value, int | float)
        and not isinstance(predicate.value, bool)
    ):
        column = f"(payload #>> {params.path(predicate.field)})::numeric"
        return f"{column} {comparison} {params.value(predicate.value)}"
    column = f"payload #>> {params.path(predicate.field)}"
    rendered = (
        str(predicate.value).lower() if isinstance(predicate.value, bool) else str(predicate.value)
    )
    return f"{column} {comparison} {params.value(rendered)}"


def _compile(plan: Query, schema: dict[str, Any], collection_id: str) -> _Compiled:
    """The main statement of each operation. `plan` is already validated.

    The LIMIT/OFFSET params are minted only on the branches that render them:
    a parameter the SQL never mentions makes `bindparams` refuse the whole
    statement.
    """
    params = _Params()
    where = _where(plan, collection_id, params, schema)
    if plan.op is QueryOp.GET:
        sql = (
            f"SELECT payload FROM collection_records WHERE {where} "
            f"ORDER BY position {_page(plan, params)}"
        )
    elif plan.op is QueryOp.PLUCK:
        assert plan.field is not None  # _validate enforced it
        sql = (
            f"SELECT payload #> {params.path(plan.field)} FROM collection_records "
            f"WHERE {where} ORDER BY position {_page(plan, params)}"
        )
    elif plan.op is QueryOp.DISTINCT:
        assert plan.field is not None
        sql = (
            f"SELECT DISTINCT payload #> {params.path(plan.field)} FROM collection_records "
            f"WHERE {where} ORDER BY 1 {_page(plan, params)}"
        )
    elif plan.group_by is not None:
        group = f"payload #>> {params.path(plan.group_by)}"
        sql = (
            f"SELECT {group} AS grp, {_aggregate_sql(plan, schema, params)} "
            f"FROM collection_records WHERE {where} "
            f"GROUP BY 1 ORDER BY 2 DESC NULLS LAST {_page(plan, params)}"
        )
    else:
        sql = f"SELECT {_aggregate_sql(plan, schema, params)} FROM collection_records WHERE {where}"
    return _Compiled(sql=sql, params=params.values, array_params=tuple(params.arrays))


def _page(plan: Query, params: _Params) -> str:
    return f"LIMIT {params.value(plan.limit)} OFFSET {params.value(plan.offset)}"


def _aggregate_sql(plan: Query, schema: dict[str, Any], params: _Params) -> str:
    if plan.op is QueryOp.COUNT:
        return "count(*)"
    assert plan.field is not None  # _validate enforced it for the other aggregates
    column = f"payload #>> {params.path(plan.field)}"
    if _is_numeric(schema, plan.field):
        column = f"({column})::numeric"
    return f"{AGGREGATES[plan.op]}({column})"


def _compile_count(plan: Query, schema: dict[str, Any], collection_id: str) -> _Compiled:
    """COUNT of everything the main statement would return without paging."""
    params = _Params()
    where = _where(plan, collection_id, params, schema)
    if plan.op is QueryOp.DISTINCT:
        assert plan.field is not None
        counted = f"count(DISTINCT payload #> {params.path(plan.field)})"
    elif plan.group_by is not None:
        counted = f"count(DISTINCT payload #>> {params.path(plan.group_by)})"
    else:
        counted = "count(*)"
    sql = f"SELECT {counted} FROM collection_records WHERE {where}"
    return _Compiled(sql=sql, params=params.values, array_params=tuple(params.arrays))


def _shape_rows(plan: Query, rows: list[tuple[Any, ...]]) -> list[Any]:
    """Bring raw driver rows into JSON-serializable Python values.

    jsonb cells arrive already deserialized — the SQLAlchemy asyncpg dialect
    registers json codecs on every connection, raw `text()` statements
    included — so GET/PLUCK/DISTINCT values pass through untouched.
    """
    if plan.op in (QueryOp.GET, QueryOp.PLUCK, QueryOp.DISTINCT):
        return [row[0] for row in rows]
    if plan.group_by is not None:
        return [{"group": row[0], "value": _plain_value(row[1])} for row in rows]
    return [_plain_value(row[0]) for row in rows]


def _plain_value(value: Any) -> Any:  # noqa: ANN401 — the driver's raw cell
    """Aggregates come back as Decimal on Postgres; JSON knows no Decimal."""
    if value is None or isinstance(value, int | float | str | bool):
        return value
    return float(value)
