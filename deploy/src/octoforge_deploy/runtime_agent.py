"""Conversation manager composition."""

from __future__ import annotations

from dataclasses import dataclass

from octoforge_core import (
    AgentLoopConfig,
    ConversationManager,
    OwnershipConfig,
    build_agent_loop,
    build_compactor,
    build_conversation_manager,
    build_cron_outcome_reporter,
    build_router,
    build_runner_config,
)
from octoforge_core.agent.prompts import PromptProvider, StaticPromptProvider
from octoforge_core.composition import RunnerOptions, RunnerServices
from octoforge_core.context.compactor import CompactorConfig, CompactorServices
from octoforge_core.tools.registry import ToolRegistry
from octoforge_server.config import Settings
from octoforge_server.prompts import FilePromptProvider

from octoforge_deploy.runtime_database import DatabaseRuntime
from octoforge_deploy.runtime_http import LanguageServices
from octoforge_deploy.runtime_knowledge import KnowledgeServices
from octoforge_deploy.runtime_media import MediaServices
from octoforge_deploy.runtime_responses import ResponseRuntime


@dataclass(frozen=True, slots=True)
class AgentBuildRequest:
    settings: Settings
    database: DatabaseRuntime
    language: LanguageServices
    knowledge: KnowledgeServices
    responses: ResponseRuntime
    media: MediaServices
    registry: ToolRegistry


def build_manager(request: AgentBuildRequest) -> ConversationManager:
    prompts: PromptProvider = FilePromptProvider(
        files=request.settings.to_prompt_files(),
        fallback=StaticPromptProvider(),
    )
    services = RunnerServices(
        loop=build_agent_loop(
            request.language.llm,
            request.registry,
            AgentLoopConfig(
                max_iterations=request.settings.agent_max_iterations,
                stream_idle_timeout=request.settings.llm_stream_idle_timeout_seconds or None,
                tool_timeout=request.settings.agent_tool_timeout_seconds,
            ),
        ),
        prompts=prompts,
        router=build_router(
            request.language.llm,
            prompts,
            timeout_seconds=request.settings.router_timeout_seconds,
        ),
        compactor=build_compactor(
            CompactorServices(
                request.knowledge.summaries,
                request.knowledge.summaries,
                request.language.llm,
                request.database.stores.limits,
            ),
            CompactorConfig(
                hot_max_chars=request.settings.context_hot_max_chars,
                compact_target_chars=request.settings.context_compact_target_chars,
                model_context_tokens=request.settings.model_context_tokens,
                context_buffer_tokens=request.settings.context_buffer_tokens,
            ),
        ),
    )
    return build_conversation_manager(
        config=build_runner_config(
            services,
            RunnerOptions(
                max_processes=request.settings.max_processes,
                material_quiet_seconds=request.settings.material_quiet_seconds,
                task_outcome_listener=build_cron_outcome_reporter(
                    request.database.stores.cron,
                ),
                vision=request.media.deep_vision,
                image_resolver=request.media.image_resolver,
                limits=request.database.stores.limits,
                response_memory=request.responses.responses.memory,
            ),
        ),
        stores=request.database.stores.manager,
        ownership=OwnershipConfig(node_id=request.settings.node_id),
    )
