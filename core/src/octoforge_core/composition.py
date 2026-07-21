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
from octoforge_core.agent.runner import ConversationManager, RunnerConfig, TaskOutcomeListener
from octoforge_core.config import LLMConfig
from octoforge_core.context.api import ContextCompactor, MessageArchive, SummaryStore
from octoforge_core.context.compactor import CompactorConfig, LlmContextCompactor
from octoforge_core.cron.api import CronStore, CronWaker, Scheduler
from octoforge_core.cron.reporter import CronOutcomeReporter
from octoforge_core.cron.scheduler import CronScheduler, CronSchedulerConfig
from octoforge_core.datasets.api import DatasetService, DatasetStore
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.db.repositories import DialogRepository, MessageRepository
from octoforge_core.instructions.api import InstructionService, InstructionStore
from octoforge_core.instructions.local import DEFAULT_RERANK_CANDIDATES, LocalInstructionService
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.llm.reranker import RerankerClient
from octoforge_core.memory.api import MemoryStore
from octoforge_core.net.external import ExternalCallAuth, ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.ports import LLMClient, TaskStore
from octoforge_core.search.api import SearchProvider
from octoforge_core.skills.base import SkillOrigin
from octoforge_core.skills.basic.cron_jobs import (
    CronCreateSkill,
    CronDeleteSkill,
    CronListSkill,
    CronPauseSkill,
    CronResumeSkill,
)
from octoforge_core.skills.basic.data_forget import DataForgetSkill
from octoforge_core.skills.basic.data_put import DataPutSkill
from octoforge_core.skills.basic.data_query import DataQuerySkill
from octoforge_core.skills.basic.external_call import ExternalCallSkill
from octoforge_core.skills.basic.history_search import HistorySearchSkill
from octoforge_core.skills.basic.http_request import HttpRequestSkill
from octoforge_core.skills.basic.instruction_save import InstructionSaveSkill
from octoforge_core.skills.basic.instructions_search import InstructionsSearchSkill
from octoforge_core.skills.basic.memory_delete import MemoryDeleteSkill
from octoforge_core.skills.basic.memory_search import MemorySearchSkill
from octoforge_core.skills.basic.memory_store import MemoryStoreSkill
from octoforge_core.skills.basic.task_list import TaskListSkill
from octoforge_core.skills.basic.task_spawn import TaskSpawnSkill
from octoforge_core.skills.basic.web_search import WebSearchSkill
from octoforge_core.skills.registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class SkillLimits:
    """All skill limit knobs in one place (mapped from the app's settings)."""

    instructions_top_k: int
    datasets_query_default_limit: int
    datasets_query_max_limit: int
    memory_search_default_limit: int
    memory_search_max_limit: int
    history_search_default_limit: int
    history_search_max_limit: int


@dataclass(frozen=True, slots=True)
class SkillStores:
    """Store ports the basic skills read and write through."""

    tasks: TaskStore
    cron: CronStore
    memory: MemoryStore
    archive: MessageArchive
    summaries: SummaryStore


@dataclass(frozen=True, slots=True)
class SkillServices:
    """Service ports the knowledge/data skills run over.

    `search_provider` is optional: without it the web_search skill is not
    registered at all.
    """

    instructions: InstructionService
    datasets: DatasetService
    executor: ExternalCallExecutor
    search_provider: SearchProvider | None = None


@dataclass(frozen=True, slots=True)
class RunnerOptions:
    """Tuning knobs of the conversation runners (one process limit, one listener)."""

    max_processes: int
    task_outcome_listener: TaskOutcomeListener | None = None


def build_llm_client(http_client: httpx.AsyncClient, config: LLMConfig) -> LLMClient:
    """Build the default LLM client (OpenAI-compatible HTTP)."""
    return OpenAICompatibleClient(http_client=http_client, config=config)


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


def build_skill_registry(
    outbound_http: httpx.AsyncClient,
    guard: SsrfGuard,
    stores: SkillStores,
    services: SkillServices,
    limits: SkillLimits,
) -> SkillRegistry:
    """Build the registry with the full set of basic skills.

    All skills are wired to the given ports; the web_search skill is
    registered only when a search provider is supplied.
    """
    registry = SkillRegistry()
    _register_core_skills(registry, outbound_http, guard, stores.tasks, stores.cron)
    if services.search_provider is not None:
        registry.register(
            WebSearchSkill(provider=services.search_provider),
            SkillOrigin.BASIC,
        )
    _register_instruction_skills(registry, services, limits)
    _register_dataset_skills(registry, services.datasets, limits)
    _register_memory_skills(registry, stores.memory, limits)
    _register_history_skill(registry, stores.archive, stores.summaries, limits)
    return registry


def build_agent_loop(
    llm: LLMClient,
    registry: SkillRegistry,
    *,
    max_iterations: int,
    stream_idle_timeout: float | None = None,
) -> AgentLoop:
    """Build the agent loop over the given LLM client and skill registry."""
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


def _register_core_skills(
    registry: SkillRegistry,
    outbound_http: httpx.AsyncClient,
    guard: SsrfGuard,
    task_store: TaskStore,
    cron_store: CronStore,
) -> None:
    """Register the HTTP, background-task and cron skills."""
    registry.register(
        HttpRequestSkill(http_client=outbound_http, guard=guard),
        SkillOrigin.BASIC,
    )
    registry.register(TaskSpawnSkill(), SkillOrigin.BASIC)
    registry.register(TaskListSkill(store=task_store), SkillOrigin.BASIC)
    registry.register(CronCreateSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronListSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronDeleteSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronPauseSkill(store=cron_store), SkillOrigin.BASIC)
    registry.register(CronResumeSkill(store=cron_store), SkillOrigin.BASIC)


def _register_instruction_skills(
    registry: SkillRegistry,
    services: SkillServices,
    limits: SkillLimits,
) -> None:
    """Register the instructions discovery/save and external-call skills."""
    registry.register(
        InstructionsSearchSkill(
            service=services.instructions,
            default_k=limits.instructions_top_k,
            datasets=services.datasets,
        ),
        SkillOrigin.BASIC,
    )
    registry.register(InstructionSaveSkill(service=services.instructions), SkillOrigin.BASIC)
    registry.register(ExternalCallSkill(executor=services.executor), SkillOrigin.BASIC)


def _register_dataset_skills(
    registry: SkillRegistry,
    datasets: DatasetService,
    limits: SkillLimits,
) -> None:
    """Register the dataset record skills."""
    registry.register(DataPutSkill(service=datasets), SkillOrigin.BASIC)
    registry.register(
        DataQuerySkill(
            service=datasets,
            default_limit=limits.datasets_query_default_limit,
            max_limit=limits.datasets_query_max_limit,
        ),
        SkillOrigin.BASIC,
    )
    registry.register(DataForgetSkill(service=datasets), SkillOrigin.BASIC)


def _register_memory_skills(
    registry: SkillRegistry,
    store: MemoryStore,
    limits: SkillLimits,
) -> None:
    """Register the memory skills over the shared memory store."""
    registry.register(MemoryStoreSkill(store=store), SkillOrigin.BASIC)
    registry.register(
        MemorySearchSkill(
            store=store,
            default_limit=limits.memory_search_default_limit,
            max_limit=limits.memory_search_max_limit,
        ),
        SkillOrigin.BASIC,
    )
    registry.register(MemoryDeleteSkill(store=store), SkillOrigin.BASIC)


def _register_history_skill(
    registry: SkillRegistry,
    archive: MessageArchive,
    summaries: SummaryStore,
    limits: SkillLimits,
) -> None:
    """Register the archive search skill over the archive/summaries read ports."""
    registry.register(
        HistorySearchSkill(
            archive=archive,
            summaries=summaries,
            default_limit=limits.history_search_default_limit,
            max_limit=limits.history_search_max_limit,
        ),
        SkillOrigin.BASIC,
    )
