"""Grouped values shared by the collection query compiler modules."""

from dataclasses import dataclass

from octoforge_core.net.collections.api import FilterPredicate, Query
from octoforge_core.net.collections.schema_infer import SchemaNode


@dataclass(frozen=True, slots=True)
class Compiled:
    """One SQL statement and all of its bound parameter metadata."""

    sql: str
    params: dict[str, object]
    array_params: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompileContext:
    """Validated query state needed by every compiler branch."""

    plan: Query
    schema: SchemaNode
    collection_id: str


@dataclass(frozen=True, slots=True)
class TotalContext:
    """Compilation state plus the size of the page already returned."""

    compile: CompileContext
    returned: int


@dataclass(frozen=True, slots=True)
class WhereContext:
    """Inputs that determine record selection."""

    compile: CompileContext
    alias: str = ""


@dataclass(frozen=True, slots=True)
class PredicateContext:
    """One predicate together with its schema and SQL alias."""

    predicate: FilterPredicate
    schema: SchemaNode
    prefix: str
