"""Agent-facing tools over stored HTTP-response collections."""

from typing import Any

from octoforge_core.net.collections.api import (
    CollectionConfig,
    CollectionError,
    CollectionStore,
    QueryEngine,
)
from octoforge_core.net.collections.ingest import render_passport
from octoforge_core.net.collections.tool_arguments import _parse_int, _parse_query, _parse_ref
from octoforge_core.net.collections.tool_contract import (
    GET_DESCRIPTION,
    GET_NAME,
    GET_SCHEMA,
    QUERY_DESCRIPTION,
    QUERY_NAME,
    QUERY_SCHEMA,
)
from octoforge_core.net.collections.tool_results import (
    NOT_FOUND_TEMPLATE,
    RESULT_TEMPLATE,
    _failure_text,
    _render_result,
)
from octoforge_core.tariffs.api import FeatureCode, feature_enabled, feature_refusal
from octoforge_core.tools.base import ToolContext, ToolSpec

__all__ = [
    "GET_DESCRIPTION",
    "GET_NAME",
    "GET_SCHEMA",
    "NOT_FOUND_TEMPLATE",
    "QUERY_DESCRIPTION",
    "QUERY_NAME",
    "QUERY_SCHEMA",
    "RESULT_TEMPLATE",
    "CollectionGetTool",
    "CollectionQueryTool",
]


class CollectionQueryTool:
    """Execute the collection query language through the engine port."""

    def __init__(self, engine: QueryEngine, config: CollectionConfig | None = None) -> None:
        self._engine = engine
        self._config = config or CollectionConfig()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=QUERY_NAME, description=QUERY_DESCRIPTION, parameters_schema=QUERY_SCHEMA
        )

    def visible_to(self, context: ToolContext) -> bool:
        """Collections exist only where HTTP calls do; use the same plan gate."""
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = _parse_ref(arguments.get("ref"))
        query = _parse_query(arguments, self._config)
        asked = _parse_int(
            arguments.get("max_chars"), self._config.query_default_chars, "max_chars"
        )
        try:
            result = await self._engine.execute(context.user_id, ref, query)
        except CollectionError as failure:
            return _failure_text(failure, arguments.get("ref"))
        return _render_result(query, result, min(asked, self._config.query_max_chars))


class CollectionGetTool:
    """Re-read a collection passport after it has fallen out of context."""

    def __init__(self, store: CollectionStore) -> None:
        self._store = store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=GET_NAME, description=GET_DESCRIPTION, parameters_schema=GET_SCHEMA)

    def visible_to(self, context: ToolContext) -> bool:
        return feature_enabled(context.enabled_features, FeatureCode.HTTP_ENDPOINTS)

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        if not self.visible_to(context):
            return feature_refusal(FeatureCode.HTTP_ENDPOINTS)
        ref = _parse_ref(arguments.get("ref"))
        try:
            passport = await self._store.passport(context.user_id, ref)
        except CollectionError as failure:
            return _failure_text(failure, arguments.get("ref"))
        return render_passport(passport)
