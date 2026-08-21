"""Agent-facing tool registry for this deployment."""

from __future__ import annotations

from dataclasses import dataclass

from octoforge_core import build_external_executor, build_tool_registry
from octoforge_core.composition import (
    ToolDependencies,
    ToolLimits,
    ToolServices,
    ToolStores,
)
from octoforge_core.mcp.api import MIRROR_KIND
from octoforge_core.net.external import (
    CallCredentials,
    ExternalCallAuth,
    ExternalCallConfig,
    ExternalCallServices,
)
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.secrets.tools import SecretLinkTool, SecretListTool
from octoforge_core.tools.registry import ToolRegistry
from octoforge_server.config import Settings
from octoforge_server.secret_links import PersonSecretsLinkBuilder

from octoforge_deploy.runtime_database import DatabaseRuntime
from octoforge_deploy.runtime_http import HttpClients, LanguageServices
from octoforge_deploy.runtime_knowledge import KnowledgeServices
from octoforge_deploy.runtime_mcp import McpRuntime
from octoforge_deploy.runtime_responses import ResponseRuntime

USER_ID_HEADER = "X-User-Id"
USER_ID_HEADER_VALUE_TEMPLATE = "{user_id}"


@dataclass(frozen=True, slots=True)
class ToolBuildRequest:
    settings: Settings
    database: DatabaseRuntime
    language: LanguageServices
    knowledge: KnowledgeServices
    responses: ResponseRuntime
    mcp: McpRuntime
    guard: SsrfGuard


def build_registry(request: ToolBuildRequest, clients: HttpClients) -> ToolRegistry:
    stores = request.database.stores
    executor = build_external_executor(
        ExternalCallServices(request.knowledge.instructions, clients.outbound, request.guard),
        ExternalCallConfig(
            credentials=CallCredentials(
                auth_whitelist=_external_call_whitelist(request.settings),
                secrets=stores.secrets,
                user_params=stores.user_params,
            ),
            delegates={MIRROR_KIND: request.mcp.call_delegate},
            spill=request.responses.responses.spill,
            truncate_chars=request.settings.http_body_max_chars,
        ),
    )
    services = ToolServices(
        instructions=request.knowledge.instructions,
        datasets=request.knowledge.datasets,
        executor=executor,
        search_provider=request.language.search,
        mcp_add=request.mcp.add_tool,
        secret_list=SecretListTool(stores.secrets) if stores.secrets is not None else None,
        secret_link=(
            SecretLinkTool(PersonSecretsLinkBuilder(request.settings, stores.secret_links))
            if stores.secrets is not None
            else None
        ),
        collections=request.responses.collections,
        responses=request.responses.responses,
    )
    return build_tool_registry(
        ToolDependencies(
            outbound_http=clients.outbound,
            guard=request.guard,
            stores=ToolStores(
                tasks=stores.manager.tasks,
                cron=stores.cron,
                archive=request.knowledge.summaries,
                summaries=request.knowledge.summaries,
            ),
            services=services,
            limit_gate=stores.limits,
        ),
        _tool_limits(request.settings),
    )


def _tool_limits(settings: Settings) -> ToolLimits:
    return ToolLimits(
        instructions_top_k=settings.instructions_top_k,
        datasets_query_default_limit=settings.datasets_query_default_limit,
        datasets_query_max_limit=settings.datasets_query_max_limit,
        history_search_default_limit=settings.history_search_default_limit,
        history_search_max_limit=settings.history_search_max_limit,
        http_request_allowed_origins=tuple(settings.http_request_allowlist),
        http_request_max_chars=settings.http_request_max_chars,
    )


def _external_call_whitelist(settings: Settings) -> tuple[ExternalCallAuth, ...]:
    self_entry = ExternalCallAuth(
        settings.self_base_url,
        USER_ID_HEADER,
        USER_ID_HEADER_VALUE_TEMPLATE,
    )
    return (*settings.to_external_call_auth_whitelist(), self_entry)
