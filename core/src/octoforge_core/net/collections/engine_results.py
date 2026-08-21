"""Conversion of Postgres driver rows into JSON-compatible query values."""

from decimal import Decimal
from typing import cast

from octoforge_core.net.collections.api import Query, QueryOp


def shape_rows(plan: Query, rows: list[tuple[object, ...]]) -> list[object]:
    """Shape raw jsonb and aggregate cells for the public result."""
    if plan.join is not None and plan.op is QueryOp.GET:
        return [{"left": row[0], "right": row[1]} for row in rows]
    if plan.op in (QueryOp.GET, QueryOp.PLUCK, QueryOp.DISTINCT):
        return [row[0] for row in rows]
    if plan.group_by is not None:
        return [{"group": row[0], "value": plain_value(row[1])} for row in rows]
    return [plain_value(row[0]) for row in rows]


def plain_value(value: object) -> object:
    """Convert Postgres Decimal aggregates to JSON's numeric types."""
    if value is None or isinstance(value, int | float | str | bool):
        return value
    return float(cast(Decimal, value))
