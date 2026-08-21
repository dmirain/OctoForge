"""Stable public boundary of the collections module."""

from octoforge_core.net.collections.collection_types import (
    REF_PREFIX,
    CollectionConfig,
    CollectionKind,
    CollectionPassport,
    NewRecords,
)
from octoforge_core.net.collections.query_types import (
    AGGREGATE_OPS,
    FIELD_OPS,
    JOIN_OPS,
    NUMERIC_OPS,
    FilterOp,
    FilterPredicate,
    JoinSpec,
    Query,
    QueryEngine,
    QueryOp,
    QueryResult,
)
from octoforge_core.net.collections.store_types import (
    CollectionAppend,
    CollectionError,
    CollectionNotFoundError,
    CollectionQueryError,
    CollectionQuotaError,
    CollectionStore,
    NewCollection,
)

__all__ = [
    "AGGREGATE_OPS",
    "FIELD_OPS",
    "JOIN_OPS",
    "NUMERIC_OPS",
    "REF_PREFIX",
    "CollectionAppend",
    "CollectionConfig",
    "CollectionError",
    "CollectionKind",
    "CollectionNotFoundError",
    "CollectionPassport",
    "CollectionQueryError",
    "CollectionQuotaError",
    "CollectionStore",
    "FilterOp",
    "FilterPredicate",
    "JoinSpec",
    "NewCollection",
    "NewRecords",
    "Query",
    "QueryEngine",
    "QueryOp",
    "QueryResult",
]
