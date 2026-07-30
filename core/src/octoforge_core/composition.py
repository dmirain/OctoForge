"""Reusable composition builders: the default wiring of the core as functions.

A composition root (the web app's `runtime()` or an installer's own) assembles
its object graph from these builders, passing ports and plain configs; any
component is overridden at the call site, without copying the whole root.
The builders depend on ports and primitives only — no FastAPI, no
application settings, no surface adapters (those stay in the web layer).
"""

from dataclasses import dataclass
from enum import StrEnum

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from octoforge_core.agent.collecting import CollectingSweeper, CollectionPromoter
from octoforge_core.agent.loop import DEFAULT_TOOL_TIMEOUT_SECONDS, AgentLoop
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
from octoforge_core.context.pg_store import PostgresSummaryStore
from octoforge_core.context.sqlite_store import SqliteSummaryStore
from octoforge_core.context.store import SqlAlchemySummaryStore
from octoforge_core.context.tools import HistorySearchTool
from octoforge_core.cron.api import CronStore, CronWaker, Scheduler
from octoforge_core.cron.reporter import CronOutcomeReporter
from octoforge_core.cron.scheduler import CronScheduler, CronSchedulerConfig
from octoforge_core.cron.tools import CronPauseTool, CronResumeTool
from octoforge_core.datasets.api import DatasetService, DatasetStore
from octoforge_core.datasets.pg_store import PostgresDatasetStore
from octoforge_core.datasets.service import LocalDatasetService
from octoforge_core.datasets.sqlite_store import SqliteDatasetStore
from octoforge_core.datasets.store import SqlAlchemyDatasetStore
from octoforge_core.datasets.tools import DataForgetTool, DataPutTool, DataQueryTool
from octoforge_core.dialogs.api import (
    DialogRepository,
    ExchangeRepository,
    MessageRepository,
)
from octoforge_core.dialogs.tools import AskUserTool
from octoforge_core.instructions.api import InstructionService, InstructionStore
from octoforge_core.instructions.local import (
    DEFAULT_RERANK_CANDIDATES,
    UNKNOWN_EMBEDDING_MODEL,
    InstructionSearchPolicy,
    LocalInstructionService,
)
from octoforge_core.instructions.pg_store import PostgresInstructionStore
from octoforge_core.instructions.sqlite_store import SqliteInstructionStore
from octoforge_core.instructions.store import SqlAlchemyInstructionStore
from octoforge_core.instructions.tools import (
    InstructionDeleteTool,
    InstructionSaveTool,
    InstructionSearchTool,
)
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.openai import OpenAICompatibleClient
from octoforge_core.llm.reranker import RerankerClient
from octoforge_core.llm.retry import RetryingLLMClient
from octoforge_core.memory.tools import MemoryDeleteTool, MemoryStoreTool
from octoforge_core.net.external import CallCredentials, ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.tools import EndpointGetTool, ExternalCallTool, HttpRequestTool
from octoforge_core.ports import LLMClient
from octoforge_core.search.api import SearchProvider
from octoforge_core.search.tools import WebSearchTool
from octoforge_core.tasks.store import TaskStore
from octoforge_core.tasks.tools import TaskCreateTool, TaskDeleteTool, TaskListTool
from octoforge_core.tools.registry import ToolRegistry
from octoforge_core.vision.api import ImageResolver, VisionClient
from octoforge_core.vision.tools import ImageLookTool


class LexicalBackend(StrEnum):
    """Which engine, if any, can answer the lexical half of a search.

    An enum rather than a bool because the two engines are not
    interchangeable: BM25 through pg_textsearch stems Russian, FTS5 through
    trigram matches substrings, and the composition root has to name which one
    it found rather than merely whether it found something.
    """

    NONE = "none"
    POSTGRES = "postgres"
    SQLITE = "sqlite"


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """All tool limit knobs in one place (mapped from the app's settings)."""

    instructions_top_k: int
    datasets_query_default_limit: int
    datasets_query_max_limit: int
    history_search_default_limit: int
    history_search_max_limit: int
    # origins `http_request` may call; empty means the open web (see net/tools.py)
    http_request_allowed_origins: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolStores:
    """Store ports the basic tools read and write through."""

    tasks: TaskStore
    cron: CronStore
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
    """Tuning knobs of the conversation runners (limit, listener, vision).

    `vision`/`image_resolver` back the strong vision tier's `image_look`
    tool (bound into `ToolContext.image_inspector` only when both are
    present — see `ConversationRunner._can_see_images`). Leaving either
    None keeps the tool hidden: the default for composition roots without a
    transport able to re-fetch an attachment's bytes.
    """

    max_processes: int
    task_outcome_listener: TaskOutcomeListener | None = None
    vision: VisionClient | None = None
    image_resolver: ImageResolver | None = None


def build_llm_client(http_client: httpx.AsyncClient, config: LLMConfig) -> LLMClient:
    """Build the default LLM client (OpenAI-compatible HTTP with retries)."""
    return RetryingLLMClient(
        OpenAICompatibleClient(http_client=http_client, config=config),
        max_retries=config.max_retries,
        base_seconds=config.retry_base_seconds,
        max_seconds=config.retry_max_seconds,
    )


def build_summary_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lexical_search: LexicalBackend,
) -> SqlAlchemySummaryStore:
    """Pick the summary/archive store: BM25-ranked history search where possible.

    Returns the concrete base class rather than a port because callers need
    both of the ports it implements (`SummaryStore` for compaction,
    `MessageArchive` for history_search) and Python has no anonymous
    intersection type.

    Without pg_textsearch `history_search` stays a substring match ordered by
    position in the dialog, which is what it always was; with it, the query is
    stemmed and results come back by relevance.
    """
    if lexical_search is LexicalBackend.POSTGRES:
        return PostgresSummaryStore(session_factory)
    if lexical_search is LexicalBackend.SQLITE:
        return SqliteSummaryStore(session_factory)
    return SqlAlchemySummaryStore(session_factory)


def build_dataset_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lexical_search: LexicalBackend,
) -> DatasetStore:
    """Pick the dataset store: BM25 over descriptions where the database can."""
    if lexical_search is LexicalBackend.POSTGRES:
        return PostgresDatasetStore(session_factory)
    if lexical_search is LexicalBackend.SQLITE:
        return SqliteDatasetStore(session_factory)
    return SqlAlchemyDatasetStore(session_factory)


def build_instruction_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    vector_search: bool,
    lexical_search: LexicalBackend = LexicalBackend.NONE,
) -> InstructionStore:
    """Pick the instruction store: pgvector-backed when the database can, else portable.

    The capability is detected with `isinstance` against
    `InstructionVectorSearch`, so it has to be a property of the class rather
    than a runtime flag — a store that always carried `search_by_vector` would
    claim vector search on a database without pgvector and fail at the first
    recall instead of at startup. `vector_search` comes from probing
    `pg_extension`, which is what makes a database without the extension a
    supported configuration rather than a crash.
    """
    if vector_search or lexical_search is LexicalBackend.POSTGRES:
        return PostgresInstructionStore(session_factory)
    if lexical_search is LexicalBackend.SQLITE:
        return SqliteInstructionStore(session_factory)
    return SqlAlchemyInstructionStore(session_factory)


def build_instruction_service(
    store: InstructionStore,
    embedder: EmbeddingClient,
    *,
    reranker: RerankerClient | None = None,
    rerank_candidates: int = DEFAULT_RERANK_CANDIDATES,
    embedding_model: str = UNKNOWN_EMBEDDING_MODEL,
) -> InstructionService:
    """Build the default instructions facade over the given store and embedder.

    `embedding_model` is the identity the service stamps on the vectors it
    writes, so a later change of `OF_EMBEDDING_MODEL` is a detectable event
    rather than a silent loss of recall. It is passed rather than read off the
    embedder because only the composition root knows which backend was wired.
    """
    return LocalInstructionService(
        store,
        embedder,
        reranker=reranker,
        policy=InstructionSearchPolicy(
            rerank_candidates=rerank_candidates,
            embedding_model=embedding_model,
        ),
    )


def build_dataset_service(store: DatasetStore, embedder: EmbeddingClient) -> DatasetService:
    """Build the default datasets facade over the given store and embedder."""
    return LocalDatasetService(store, embedder)


def build_external_executor(
    service: InstructionService,
    http_client: httpx.AsyncClient,
    guard: SsrfGuard,
    credentials: CallCredentials | None = None,
) -> ExternalCallExecutor:
    """Build the executor of external calls described by tool records."""
    return ExternalCallExecutor(
        service=service,
        http_client=http_client,
        guard=guard,
        credentials=credentials,
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
    registered only when a search provider is supplied. `image_look` is
    always registered — its own `visible_to` hook hides it per dialog when
    `ToolContext.image_inspector` is None (no vision client/resolver wired
    into that dialog's `RunnerConfig`), so no conditional registration is
    needed here.
    """
    registry = ToolRegistry()
    _register_core_tools(registry, outbound_http, guard, stores, limits)
    registry.register(ImageLookTool())
    if services.search_provider is not None:
        registry.register(WebSearchTool(provider=services.search_provider))
    _register_instruction_tools(registry, services, limits)
    _register_dataset_tools(registry, services.datasets, limits)
    _register_memory_tools(registry, services.instructions)
    _register_history_tool(registry, stores.archive, stores.summaries, limits)
    return registry


def build_agent_loop(
    llm: LLMClient,
    registry: ToolRegistry,
    *,
    max_iterations: int,
    stream_idle_timeout: float | None = None,
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> AgentLoop:
    """Build the agent loop over the given LLM client and tool registry."""
    return AgentLoop(
        llm_client=llm,
        registry=registry,
        max_iterations=max_iterations,
        stream_idle_timeout=stream_idle_timeout,
        tool_timeout=tool_timeout,
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


def build_cron_outcome_reporter(store: CronStore) -> TaskOutcomeListener:
    """Build the listener folding fired cron-process outcomes back into the store."""
    return CronOutcomeReporter(store)


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
        vision=options.vision,
        image_resolver=options.image_resolver,
    )


def build_conversation_manager(
    config: RunnerConfig,
    dialogs: DialogRepository,
    messages: MessageRepository,
    tasks: TaskStore,
    exchanges: ExchangeRepository,
) -> ConversationManager:
    """Build the conversation manager owning one runner per dialog."""
    return ConversationManager(
        config=config,
        dialogs=dialogs,
        messages=messages,
        tasks=tasks,
        exchanges=exchanges,
    )


def build_cron_scheduler(
    store: CronStore,
    waker: CronWaker,
    *,
    owner: str,
    config: CronSchedulerConfig,
) -> Scheduler:
    """Build the default cron scheduler; starting `run_forever()` is the caller's job."""
    return CronScheduler(store=store, waker=waker, owner=owner, config=config)


def build_collecting_sweeper(
    exchanges: ExchangeRepository,
    dialogs: DialogRepository,
    promoter: CollectionPromoter,
    *,
    quiet_seconds: float,
    interval_seconds: float,
) -> CollectingSweeper:
    """Build the material-collection sweep; starting `run_forever()` is the caller's job."""
    return CollectingSweeper(
        exchanges=exchanges,
        dialogs=dialogs,
        promoter=promoter,
        quiet_seconds=quiet_seconds,
        interval_seconds=interval_seconds,
    )


def _register_core_tools(
    registry: ToolRegistry,
    outbound_http: httpx.AsyncClient,
    guard: SsrfGuard,
    stores: ToolStores,
    limits: ToolLimits,
) -> None:
    """Register the HTTP and deferred-work (task/cron) tools."""
    registry.register(
        HttpRequestTool(
            http_client=outbound_http,
            guard=guard,
            allowed_origins=limits.http_request_allowed_origins,
        )
    )
    registry.register(AskUserTool())
    registry.register(TaskCreateTool(cron_store=stores.cron))
    registry.register(TaskListTool(store=stores.tasks, cron_store=stores.cron))
    registry.register(TaskDeleteTool(store=stores.tasks, cron_store=stores.cron))
    registry.register(CronPauseTool(store=stores.cron))
    registry.register(CronResumeTool(store=stores.cron))


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
    registry.register(EndpointGetTool(service=services.instructions))
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
    instructions: InstructionService,
) -> None:
    """Register the memory write tools over the shared instruction store.

    Reading memories is instruction_search's job (memories are ranked with
    everything else), so no memory_search tool is registered.
    """
    registry.register(MemoryStoreTool(service=instructions))
    registry.register(MemoryDeleteTool(service=instructions))


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
