"""Typed dependency groups shared by the core composition builders."""

from dataclasses import dataclass
from enum import StrEnum

import httpx

from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import PromptProvider
from octoforge_core.agent.router import MessageRouter
from octoforge_core.agent.runner import TaskOutcomeListener
from octoforge_core.context.api import ContextCompactor, MessageArchive, SummaryStore
from octoforge_core.cron.api import CronStore
from octoforge_core.datasets.api import DatasetService
from octoforge_core.instructions.api import InstructionService
from octoforge_core.instructions.local import DEFAULT_RERANK_CANDIDATES, UNKNOWN_EMBEDDING_MODEL
from octoforge_core.llm.reranker import RerankerClient
from octoforge_core.net.external import ExternalCallExecutor
from octoforge_core.net.guard import SsrfGuard
from octoforge_core.net.http_request_types import MAX_RESPONSE_CHARS
from octoforge_core.search.api import SearchProvider
from octoforge_core.tariffs.api import LimitGate
from octoforge_core.tasks.store import TaskStore
from octoforge_core.tools.base import Tool
from octoforge_core.tools.responses import TaskScopedResponses
from octoforge_core.vision.api import ImageResolver, VisionClient

from .composition_responses import CollectionsRuntime, ResponseLayer


class LexicalBackend(StrEnum):
    """Database engine available for lexical search."""

    NONE = "none"
    POSTGRES = "postgres"
    SQLITE = "sqlite"


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """All tool limit knobs in one place."""

    instructions_top_k: int
    datasets_query_default_limit: int
    datasets_query_max_limit: int
    history_search_default_limit: int
    history_search_max_limit: int
    http_request_allowed_origins: tuple[str, ...] = ()
    http_request_max_chars: int = MAX_RESPONSE_CHARS


@dataclass(frozen=True, slots=True)
class ToolStores:
    """Store ports used by the code tools."""

    tasks: TaskStore
    cron: CronStore
    archive: MessageArchive
    summaries: SummaryStore


@dataclass(frozen=True, slots=True)
class ToolServices:
    """Domain services used by the code tools."""

    instructions: InstructionService
    datasets: DatasetService
    executor: ExternalCallExecutor
    search_provider: SearchProvider | None = None
    mcp_add: Tool | None = None
    secret_list: Tool | None = None
    secret_link: Tool | None = None
    collections: CollectionsRuntime | None = None
    responses: ResponseLayer | None = None


@dataclass(frozen=True, slots=True)
class ToolDependencies:
    """Runtime collaborators borrowed by a complete tool registry."""

    outbound_http: httpx.AsyncClient
    guard: SsrfGuard
    stores: ToolStores
    services: ToolServices
    limit_gate: LimitGate | None = None


@dataclass(frozen=True, slots=True)
class InstructionServiceOptions:
    """Ranking policy and vector identity of the instruction service."""

    reranker: RerankerClient | None = None
    rerank_candidates: int = DEFAULT_RERANK_CANDIDATES
    embedding_model: str = UNKNOWN_EMBEDDING_MODEL


@dataclass(frozen=True, slots=True)
class RunnerServices:
    """Behavior services shared by every runner in one manager."""

    loop: AgentLoop
    prompts: PromptProvider
    router: MessageRouter
    compactor: ContextCompactor


@dataclass(frozen=True, slots=True)
class RunnerOptions:
    """Optional runner capabilities and process limits."""

    max_processes: int
    task_outcome_listener: TaskOutcomeListener | None = None
    vision: VisionClient | None = None
    image_resolver: ImageResolver | None = None
    limits: LimitGate | None = None
    response_memory: TaskScopedResponses | None = None
