"""OctoForge agent core library."""

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    ProcessCompleted,
    ProcessSuspended,
    RetryScheduled,
    TextDelta,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import PromptProvider
from octoforge_core.agent.router import MessageRouter
from octoforge_core.agent.runner import (
    ConversationEvent,
    ConversationManager,
    ConversationRunner,
    RunnerConfig,
    TaskOutcomeListener,
)
from octoforge_core.composition import (
    RunnerOptions,
    ToolLimits,
    ToolServices,
    ToolStores,
    build_agent_loop,
    build_compactor,
    build_conversation_manager,
    build_cron_outcome_reporter,
    build_cron_scheduler,
    build_dataset_service,
    build_external_executor,
    build_instruction_service,
    build_llm_client,
    build_router,
    build_runner_config,
    build_tool_registry,
)
from octoforge_core.config import EmbeddingConfig, LLMConfig
from octoforge_core.context.api import ContextCompactor, MessageArchive, SummaryStore
from octoforge_core.cron.api import CronStore, CronWaker, Scheduler
from octoforge_core.datasets.api import DatasetService, DatasetStore, DatasetVectorSearch
from octoforge_core.db.engine import (
    bootstrap_schema,
    create_engine,
    create_session_factory,
    init_db,
)
from octoforge_core.db.errors import DialogNotFoundError
from octoforge_core.db.repositories import DialogRepository, MessageRepository, SqlAlchemyTaskStore
from octoforge_core.domain import ChatMessage, Dialog, MessageRole, ToolCall
from octoforge_core.errors import LLMResponseError
from octoforge_core.instructions.api import (
    InstructionService,
    InstructionStore,
    InstructionVectorSearch,
)
from octoforge_core.llm.embeddings import EmbeddingClient
from octoforge_core.llm.errors import (
    AuthError,
    ClientError,
    ContextOverflowError,
    ErrorKind,
    LLMError,
    ProviderInternalError,
    QuotaError,
    RateLimitError,
    TransportError,
)
from octoforge_core.llm.reranker import RerankerClient
from octoforge_core.llm.retry import RetryingLLMClient
from octoforge_core.llm.usage import Completion, Usage
from octoforge_core.memory.api import MemoryStore
from octoforge_core.ports import LLMClient
from octoforge_core.search.api import SearchProvider
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.spawner import TaskDeleteOutcome, TaskDeleter, TaskSpawner
from octoforge_core.tasks.store import TaskStore
from octoforge_core.time import utc_now
from octoforge_core.tools.base import Tool, ToolContext, ToolSpec
from octoforge_core.tools.errors import (
    DuplicateToolError,
    ToolArgumentsError,
    ToolNotFoundError,
)
from octoforge_core.tools.registry import ToolRegistry

__all__ = [
    "AgentLoop",
    "AssistantMessage",
    "AuthError",
    "Cancelled",
    "ChatMessage",
    "ClientError",
    "Completion",
    "ContextCompactor",
    "ContextOverflowError",
    "ConversationEvent",
    "ConversationManager",
    "ConversationRunner",
    "CronStore",
    "CronWaker",
    "DatasetService",
    "DatasetStore",
    "DatasetVectorSearch",
    "Dialog",
    "DialogNotFoundError",
    "DialogRepository",
    "DuplicateToolError",
    "EmbeddingClient",
    "EmbeddingConfig",
    "ErrorKind",
    "Failed",
    "Finished",
    "InstructionService",
    "InstructionStore",
    "InstructionVectorSearch",
    "IterationStarted",
    "LLMClient",
    "LLMConfig",
    "LLMError",
    "LLMResponseError",
    "LoopControl",
    "LoopEvent",
    "MemoryStore",
    "MessageArchive",
    "MessageRepository",
    "MessageRole",
    "MessageRouter",
    "ProcessCompleted",
    "ProcessSuspended",
    "PromptProvider",
    "ProviderInternalError",
    "QuotaError",
    "RateLimitError",
    "RerankerClient",
    "RetryScheduled",
    "RetryingLLMClient",
    "RunnerConfig",
    "RunnerOptions",
    "Scheduler",
    "SearchProvider",
    "SqlAlchemyTaskStore",
    "SummaryStore",
    "Task",
    "TaskDeleteOutcome",
    "TaskDeleter",
    "TaskKind",
    "TaskOutcomeListener",
    "TaskSpawner",
    "TaskStatus",
    "TaskStore",
    "TextDelta",
    "Tool",
    "ToolArgumentsError",
    "ToolCall",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ToolCallRequested",
    "ToolContext",
    "ToolLimits",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolServices",
    "ToolSpec",
    "ToolStores",
    "TransportError",
    "Usage",
    "bootstrap_schema",
    "build_agent_loop",
    "build_compactor",
    "build_conversation_manager",
    "build_cron_outcome_reporter",
    "build_cron_scheduler",
    "build_dataset_service",
    "build_external_executor",
    "build_instruction_service",
    "build_llm_client",
    "build_router",
    "build_runner_config",
    "build_tool_registry",
    "create_engine",
    "create_session_factory",
    "init_db",
    "utc_now",
]
