"""Reusable composition builders: the default wiring of the core as functions.

A composition root (the web app's `runtime()` or an installer's own) assembles
its object graph from these builders, passing ports and plain configs; any
component is overridden at the call site, without copying the whole root.
The builders depend on ports and primitives only — no FastAPI, no
application settings, no surface adapters (those stay in the web layer).
"""

from dataclasses import dataclass

import httpx

from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import PromptProvider
from octoforge_core.agent.router import LLMRouter, MessageRouter
from octoforge_core.agent.runner import (
    ConversationManager,
    RunnerConfig,
    TaskOutcomeListener,
)
from octoforge_core.config import LLMConfig
from octoforge_core.context.api import ContextCompactor, MessageArchive, SummaryStore
from octoforge_core.context.compactor import CompactorConfig, LlmContextCompactor
from octoforge_core.context.tools import HistorySearchTool
from octoforge_core.cron.api import CronStore, CronWaker, Scheduler
from octoforge_core.cron.reporter import CronOutcomeReporter
from octoforge_core.cron.scheduler import CronScheduler, CronSchedulerConfig
from octoforge_core.cron.tools import CronPauseTool, CronResumeTool
from octoforge_core.datasets.api import DatasetService, DatasetStore
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.datasets.tools import DataForgetTool, DataPutTool, DataQueryTool
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.instructions.api import InstructionService, InstructionStore
from octoforge_core.instructions.local import DEFAULT_RERANK_CANDIDATES, LocalInstructionService
from octoforge_core.instructions.tools import (
    InstructionDeleteTool,
    InstructionSaveTool,
    InstructionSearchTool,
)
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.llm.reranker import RerankerClient
from octoforge_core.llm.retry import RetryingLLMClient
from octoforge_core.memory.api import MemoryStore
from octoforge_core.memory.tools import MemoryDeleteTool, MemorySearchTool, MemoryStoreTool
from octoforge_core.net.external import ExternalCallAuth, ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tools import ExternalCallTool, HttpRequestTool
from octoforge_core.ports import LLMClient
from octoforge_core.search.api import SearchProvider
from octoforge_core.search.tools import WebSearchTool
from octoforge_core.tasks.store import TaskStore
from octoforge_core.tasks.tools import TaskCreateTool, TaskDeleteTool, TaskListTool
from octoforge_core.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """All tool limit knobs in one place (mapped from the app's settings)."""

    instructions_top_k: int
    datasets_query_default_limit: int
    datasets_query_max_limit: int
    memory_search_default_limit: int
    memory_search_max_limit: int
    history_search_default_limit: int
    history_search_max_limit: int


@dataclass(frozen=True, slots=True)
class ToolStores:
    """Store ports the basic tools read and write through."""

    tasks: TaskStore
    cron: CronStore
    memory: MemoryStore
    archive: MessageArchive
    summaries: SummaryStore


@dataclass(frozen=True, slots=True)
class ToolServices:
    """Service ports the knowledge/data tools run over.

    `search_provider` is optional: without it the web_search tool is not
    registered at all.
    """

    instructions: InstructionService
    datasets: DatasetService
    executor: ExternalCallExecutor
    search_provider: SearchProvider | None = None


@dataclass(frozen=True, slots=True)
class RunnerOptions:
    """Tuning knobs of the conversation runners (limit, listener)."""

    max_processes: int
    task_outcome_listener: TaskOutcomeListener | None = None


def build_llm_client(http_client: httpx.AsyncClient, config: LLMConfig) -> LLMClient:
    """Build the default LLM client (OpenAI-compatible HTTP with retries)."""
    return RetryingLLMClient(
        OpenAICompatibleClient(http_client=http_client, config=config),
        max_retries=config.max_retries,
        base_seconds=config.retry_base_seconds,
        max_seconds=config.retry_max_seconds,
    )


def build_instruction_service(
    store: InstructionStore,
    embedder: EmbeddingClient,
    *,
    reranker: RerankerClient | None = None,
    rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
) -> InstructionService:
    """Build the default instructions facade over the given store and embedder."""
    return LocalInstructionService(
        store,
        embedder,
        reranker=reranker,
        rerank_candidates=rerank_candidates,
    )


def build_dataset_service(store: DatasetStore, embedder: EmbeddingClient) -> DatasetService:
    """Build the default datasets facade over the given store and embedder."""
    return LocalDatasetService(store, embedder)


def build_external_executor(
    service: InstructionService,
    http_client: httpx.AsyncClient,
    guard: SsrfGuard,
    auth_whitelist: tuple[ExternalCallAuth, ...] = (),
) -> ExternalCallExecutor:
    """Build the executor of external calls described by tool records."""
    return ExternalCallExecutor(
        service=service,
        http_client=http_client,
        guard=guard,
        auth_whitelist=auth_whitelist,
    )


def build_tool_registry(
    outbound_http: httpx.AsyncClient,
    guard: SsrfGuard,
    stores: ToolStores,
    services: ToolServices,
    limits: ToolLimits,
) -> ToolRegistry:
    """Build the registry with the full set of code tools.

    All tools are wired to the given ports; the web_search tool is
    registered only when a search provider is supplied.
    """
    registry = ToolRegistry()
    _register_core_tools(registry, outbound_http, guard, stores.tasks, stores.cron)
    if services.search_provider is not None:
        registry.register(WebSearchTool(provider=services.search_provider))
    _register_instruction_tools(registry, services, limits)
    _register_dataset_tools(registry, services.datasets, limits)
    _register_memory_tools(registry, stores.memory, limits)
    _register_history_tool(registry, stores.archive, stores.summaries, limits)
    return registry


def build_agent_loop(
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    max_iterations: int,
    stream_idle_timeout: float | None = None,
) -> AgentLoop:
    """Build the agent loop over the given LLM client and tool registry."""
    return AgentLoop(
        llm_client=llm,
        registry=registry,
        max_iterations=max_iterations,
        stream_idle_timeout=stream_idle_timeout,
    )


def build_compactor(
    store: SummaryStore,
    archive: MessageArchive,
    llm: LLMClient,
    config: CompactorConfig,
) -> ContextCompactor:
    """Build the default context compactor (topics block + hot tail)."""
    return LlmContextCompactor(store=store, archive=archive, llm=llm, config=config)


def build_router(
    llm: LLMClient,
    prompts: PromptProvider,
    *,
    timeout_seconds: float,
) -> MessageRouter:
    """Build the default LLM message router."""
    return LLMRouter(llm, timeout_seconds=timeout_seconds, prompts=prompts)


def build_cron_outcome_reporter(
    store: CronStore,
    *,
    retry_limit: int,
    backoff_base_seconds: float,
) -> TaskOutcomeListener:
    """Build the listener folding fired cron-process outcomes back into the store."""
    return CronOutcomeReporter(
        store,
        retry_limit=retry_limit,
        backoff_base_seconds=backoff_base_seconds,
    )


def build_runner_config(
    loop: AgentLoop,
    prompts: PromptProvider,
    router: MessageRouter,
    compactor: ContextCompactor,
    options: RunnerOptions,
) -> RunnerConfig:
    """Build the runner behavior config shared by one conversation manager."""
    return RunnerConfig(
        loop=loop,
        prompts=prompts,
        router=router,
        max_processes=options.max_processes,
        compactor=compactor,
        task_outcome_listener=options.task_outcome_listener,
    )


def build_conversation_manager(
    config: RunnerConfig,
    dialogs: DialogRepository,
    messages: MessageRepository,
    tasks: TaskStore,
) -> ConversationManager:
    """Build the conversation manager owning one runner per dialog."""
    return ConversationManager(config=config, dialogs=dialogs, messages=messages, tasks=tasks)


def build_cron_scheduler(
    store: CronStore,
    waker: CronWaker,
    *,
    owner: str,
    config: CronSchedulerConfig,
) -> Scheduler:
    """Build the default cron scheduler; starting `run_forever()` is the caller's job."""
    return CronScheduler(store=store, waker=waker, owner=owner, config=config)


def _register_core_tools(
    registry: ToolRegistry,
    outbound_http: httpx.AsyncClient,
    guard: SsrfGuard,
    task_store: TaskStore,
    cron_store: CronStore,
) -> None:
    """Register the HTTP and deferred-work (task/cron) tools."""
    registry.register(HttpRequestTool(http_client=outbound_http, guard=guard))
    registry.register(TaskCreateTool(cron_store=cron_store))
    registry.register(TaskListTool(store=task_store, cron_store=cron_store))
    registry.register(TaskDeleteTool(store=task_store, cron_store=cron_store))
    registry.register(CronPauseTool(store=cron_store))
    registry.register(CronResumeTool(store=cron_store))


def _register_instruction_tools(
    registry: ToolRegistry,
    services: ToolServices,
    limits: ToolLimits,
) -> None:
    """Register the instructions search/save/delete and external-call tools."""
    registry.register(
        InstructionSearchTool(
            service=services.instructions,
            default_k=limits.instructions_top_k,
            datasets=services.datasets,
        )
    )
    registry.register(InstructionSaveTool(service=services.instructions))
    registry.register(InstructionDeleteTool(service=services.instructions))
    registry.register(ExternalCallTool(executor=services.executor))


def _register_dataset_tools(
    registry: ToolRegistry,
    datasets: DatasetService,
    limits: ToolLimits,
) -> None:
    """Register the dataset record tools."""
    registry.register(DataPutTool(service=datasets))
    registry.register(
        DataQueryTool(
            service=datasets,
            default_limit=limits.datasets_query_default_limit,
            max_limit=limits.datasets_query_max_limit,
        )
    )
    registry.register(DataForgetTool(service=datasets))


def _register_memory_tools(
    registry: ToolRegistry,
    store: MemoryStore,
    limits: ToolLimits,
) -> None:
    """Register the memory tools over the shared memory store."""
    registry.register(MemoryStoreTool(store=store))
    registry.register(
        MemorySearchTool(
            store=store,
            default_limit=limits.memory_search_default_limit,
            max_limit=limits.memory_search_max_limit,
        )
    )
    registry.register(MemoryDeleteTool(store=store))


def _register_history_tool(
    registry: ToolRegistry,
    archive: MessageArchive,
    summaries: SummaryStore,
    limits: ToolLimits,
) -> None:
    """Register the archive search tool over the archive/summaries read ports."""
    registry.register(
        HistorySearchTool(
            archive=archive,
            summaries=summaries,
            default_limit=limits.history_search_default_limit,
            max_limit=limits.history_search_max_limit,
        )
    )
