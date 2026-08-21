"""Collections and response-memory composition."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from octoforge_core.net.collections.api import CollectionConfig
from octoforge_core.net.collections.documents import DatabaseDocumentHome
from octoforge_core.net.collections.engine import PostgresCollectionQueryEngine
from octoforge_core.net.collections.ingest import ResponseSpill, ResponseSpillOptions
from octoforge_core.net.collections.store import SqlAlchemyCollectionStore
from octoforge_core.net.collections.tools import CollectionGetTool, CollectionQueryTool
from octoforge_core.net.response_memory import (
    DocumentHome,
    ResponseFindTool,
    ResponseGetTool,
    ResponseMemory,
    ResponseMemoryConfig,
    ResponseWindowTool,
)


@dataclass(frozen=True, slots=True)
class CollectionsRuntime:
    """Postgres-backed collection store and its tools."""

    store: SqlAlchemyCollectionStore
    query_tool: CollectionQueryTool
    get_tool: CollectionGetTool


@dataclass(frozen=True, slots=True)
class ResponseLayer:
    """Response spill router, memory and reading tools."""

    memory: ResponseMemory
    spill: ResponseSpill
    get_tool: ResponseGetTool
    find_tool: ResponseFindTool
    window_tool: ResponseWindowTool


def build_collections(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    config: CollectionConfig,
) -> CollectionsRuntime | None:
    """Build the database tier only where jsonb queries are supported."""
    if engine.dialect.name != "postgresql":
        return None
    store = SqlAlchemyCollectionStore(sessions, config)
    query = PostgresCollectionQueryEngine(sessions, config)
    return CollectionsRuntime(store, CollectionQueryTool(query, config), CollectionGetTool(store))


def build_response_layer(
    collections: CollectionsRuntime | None,
    config: CollectionConfig,
    memory_config: ResponseMemoryConfig | None = None,
) -> ResponseLayer:
    """Build task memory plus the shape-aware response spill router."""
    memory = ResponseMemory(memory_config)
    home: DocumentHome = (
        DatabaseDocumentHome(collections.store, config, memory.config)
        if collections is not None
        else memory
    )
    spill = ResponseSpill(
        ResponseSpillOptions(
            collections.store if collections is not None else None,
            config,
            memory,
            home,
        )
    )
    return ResponseLayer(
        memory,
        spill,
        ResponseGetTool(home, memory.config),
        ResponseFindTool(home, memory.config),
        ResponseWindowTool(home, memory.config),
    )
