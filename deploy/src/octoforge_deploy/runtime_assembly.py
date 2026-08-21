"""Staged construction of the deployment object graph."""

from __future__ import annotations

from dataclasses import dataclass

from octoforge_core.net.guard import SsrfGuard
from octoforge_server.config import Settings
from octoforge_telegram.config import TelegramSettings

from octoforge_deploy.runtime_agent import AgentBuildRequest, build_manager
from octoforge_deploy.runtime_database import DatabaseRuntime
from octoforge_deploy.runtime_http import HttpClients, build_language_services
from octoforge_deploy.runtime_knowledge import build_knowledge_services
from octoforge_deploy.runtime_mcp import McpBuildRequest, build_mcp
from octoforge_deploy.runtime_media import MediaBuildRequest, build_media_services
from octoforge_deploy.runtime_responses import build_response_runtime
from octoforge_deploy.runtime_state import RuntimeGraph
from octoforge_deploy.runtime_surface_assembly import (
    SurfaceBuildRequest,
    build_surfaces,
)
from octoforge_deploy.runtime_tools import ToolBuildRequest, build_registry


@dataclass(frozen=True, slots=True)
class GraphBuildRequest:
    settings: Settings
    telegram: TelegramSettings
    database: DatabaseRuntime


async def build_runtime_graph(
    request: GraphBuildRequest,
    clients: HttpClients,
) -> RuntimeGraph:
    settings, database = request.settings, request.database
    language = build_language_services(settings, clients)
    media = build_media_services(
        MediaBuildRequest(settings, request.telegram, database.stores.limits),
        clients,
    )
    knowledge = await build_knowledge_services(settings, database, language)
    responses = build_response_runtime(settings, database)
    guard = SsrfGuard(allowed_prefixes=(settings.self_base_url,))
    mcp = build_mcp(
        McpBuildRequest(
            database.sessions,
            guard,
            knowledge.instructions,
            database.stores.secrets,
            language.llm,
            settings,
            responses,
        ),
        clients,
    )
    registry = build_registry(
        ToolBuildRequest(settings, database, language, knowledge, responses, mcp, guard),
        clients,
    )
    manager = build_manager(
        AgentBuildRequest(settings, database, language, knowledge, responses, media, registry)
    )
    surfaces = await build_surfaces(
        SurfaceBuildRequest(
            settings,
            request.telegram,
            database,
            knowledge,
            media,
            manager,
            registry,
        ),
        clients,
    )
    return RuntimeGraph(
        settings,
        request.telegram,
        database,
        clients,
        language,
        knowledge,
        responses,
        media,
        mcp,
        registry,
        manager,
        surfaces,
    )
