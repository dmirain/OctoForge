"""Bound parameter and SQLAlchemy statement support for query compilation."""

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.sql.elements import TextClause

from octoforge_core.net.collections.engine_types import Compiled


class Params:
    """Accumulate bound values while remembering text-array field paths."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.arrays: list[str] = []

    def path(self, dotted: str) -> str:
        name = self._name()
        self.values[name] = dotted.split(".")
        self.arrays.append(name)
        return f":{name}"

    def value(self, value: object) -> str:
        name = self._name()
        self.values[name] = value
        return f":{name}"

    def compiled(self, sql: str) -> Compiled:
        return Compiled(sql, self.values, tuple(self.arrays))

    def _name(self) -> str:
        return f"p{len(self.values)}"


def statement(compiled: Compiled) -> TextClause:
    """Build a typed executable and mark path parameters as Postgres text arrays."""
    binds = [
        bindparam(name, value=value, type_=ARRAY(Text()))
        if name in compiled.array_params
        else bindparam(name, value=value)
        for name, value in compiled.params.items()
    ]
    return text(compiled.sql).bindparams(*binds)


def page(plan_limit: tuple[int, int], params: Params) -> str:
    """Render LIMIT/OFFSET only on branches that use their parameters."""
    limit, offset = plan_limit
    return f"LIMIT {params.value(limit)} OFFSET {params.value(offset)}"
