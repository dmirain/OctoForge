"""The validated object-language used to query collections."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class QueryOp(StrEnum):
    GET = "get"
    PLUCK = "pluck"
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    DISTINCT = "distinct"


AGGREGATE_OPS = frozenset({QueryOp.COUNT, QueryOp.SUM, QueryOp.AVG, QueryOp.MIN, QueryOp.MAX})
FIELD_OPS = frozenset(
    {QueryOp.PLUCK, QueryOp.SUM, QueryOp.AVG, QueryOp.MIN, QueryOp.MAX, QueryOp.DISTINCT}
)
NUMERIC_OPS = frozenset({QueryOp.SUM, QueryOp.AVG})
JOIN_OPS = frozenset({"get", "count"})


class FilterOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    CONTAINS = "contains"


@dataclass(frozen=True, slots=True)
class FilterPredicate:
    field: str
    op: FilterOp
    value: str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class JoinSpec:
    ref: str
    on_left: str
    on_right: str
    source: str | None = None


@dataclass(frozen=True, slots=True)
class Query:
    op: QueryOp
    field: str | None = None
    filters: tuple[FilterPredicate, ...] = ()
    group_by: str | None = None
    source: str | None = None
    join: JoinSpec | None = None
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True, slots=True)
class QueryResult:
    rows: list[Any]
    total: int


class QueryEngine(Protocol):
    async def execute(self, owner_id: str, collection_id: str, query: Query) -> QueryResult: ...
