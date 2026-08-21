"""Postgres collection query execution over the validated SQL compiler."""

import json
from dataclasses import dataclass
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.db.unit_of_work import read_session
from octoforge_core.net.collections.api import (
    AGGREGATE_OPS,
    CollectionConfig,
    CollectionNotFoundError,
    Query,
    QueryResult,
)
from octoforge_core.net.collections.engine_compile import compile_query
from octoforge_core.net.collections.engine_count import compile_count
from octoforge_core.net.collections.engine_results import shape_rows
from octoforge_core.net.collections.engine_sql import statement
from octoforge_core.net.collections.engine_types import CompileContext, TotalContext
from octoforge_core.net.collections.engine_validation import ValidationContext, validate
from octoforge_core.net.collections.schema_infer import SchemaNode
from octoforge_core.time import utc_now


@dataclass(frozen=True, slots=True)
class _CollectionKey:
    owner_id: str
    collection_id: str


class PostgresCollectionQueryEngine:
    """Compile and execute collection queries against the two fixed tables."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: CollectionConfig | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config or CollectionConfig()

    async def execute(self, owner_id: str, collection_id: str, query: Query) -> QueryResult:
        """Validate against the derived schema, compile, and run."""
        async with read_session(self._session_factory) as session:
            key = _CollectionKey(owner_id, collection_id)
            schema = await self._record_schema(session, key)
            right_schema = await self._right_schema(session, owner_id, query)
            validation = ValidationContext(query, schema, right_schema, self._config)
            plan = validate(validation)
            context = CompileContext(plan, schema, collection_id)
            result = await session.execute(statement(compile_query(context)))
            rows = [tuple(row) for row in result.all()]
            total = await self._total(session, TotalContext(context, len(rows)))
        return QueryResult(rows=shape_rows(plan, rows), total=total)

    async def _right_schema(
        self, session: AsyncSession, owner_id: str, query: Query
    ) -> SchemaNode | None:
        if query.join is None:
            return None
        key = _CollectionKey(owner_id, query.join.ref)
        return await self._record_schema(session, key)

    async def _record_schema(self, session: AsyncSession, key: _CollectionKey) -> SchemaNode:
        """Load a schema while hiding expired and foreign collections."""
        query = text(
            "SELECT owner_id, expires_at, schema FROM collections WHERE id = :cid"
        ).bindparams(cid=key.collection_id)
        found = (await session.execute(query)).first()
        if found is None or found[0] != key.owner_id or found[1] <= utc_now():
            raise CollectionNotFoundError(key.collection_id)
        raw = found[2]
        if isinstance(raw, dict):
            return cast(SchemaNode, dict(raw))
        return cast(SchemaNode, dict(json.loads(raw)))

    async def _total(self, session: AsyncSession, context: TotalContext) -> int:
        """Count unpaged row results only when the returned page cannot answer."""
        plan = context.compile.plan
        if plan.op in AGGREGATE_OPS and plan.group_by is None:
            return 1
        if plan.offset == 0 and context.returned < plan.limit:
            return context.returned
        counted = (await session.execute(statement(compile_count(context.compile)))).scalar()
        return int(counted or 0)
