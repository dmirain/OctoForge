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
    ProcessResumed,
    ProcessSuspended,
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
    SkillLimits,
    SkillServices,
    SkillStores,
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
    build_skill_registry,
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
from octoforge_core.llm.reranker import RerankerClient
from octoforge_core.memory.api import MemoryStore
from octoforge_core.ports import LLMClient, TaskStore
from octoforge_core.search.api import SearchProvider
from octoforge_core.skills.base import Skill, SkillContext, SkillOrigin, SkillSpec
from octoforge_core.skills.errors import (
    DuplicateSkillError,
    SkillArgumentsError,
    SkillNotFoundError,
)
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.tasks.spawner import TaskSpawner
from octoforge_core.time import utc_now

__all__ = [
    "AgentLoop",
    "AssistantMessage",
    "Cancelled",
    "ChatMessage",
    "ContextCompactor",
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
    "DuplicateSkillError",
    "EmbeddingClient",
    "EmbeddingConfig",
    "Failed",
    "Finished",
    "InstructionService",
    "InstructionStore",
    "InstructionVectorSearch",
    "IterationStarted",
    "LLMClient",
    "LLMConfig",
    "LLMResponseError",
    "LoopControl",
    "LoopEvent",
    "MemoryStore",
    "MessageArchive",
    "MessageRepository",
    "MessageRole",
    "MessageRouter",
    "ProcessCompleted",
    "ProcessResumed",
    "ProcessSuspended",
    "PromptProvider",
    "RerankerClient",
    "RunnerConfig",
    "RunnerOptions",
    "Scheduler",
    "SearchProvider",
    "Skill",
    "SkillArgumentsError",
    "SkillContext",
    "SkillLimits",
    "SkillNotFoundError",
    "SkillOrigin",
    "SkillRegistry",
    "SkillServices",
    "SkillSpec",
    "SkillStores",
    "SqlAlchemyTaskStore",
    "SummaryStore",
    "Task",
    "TaskKind",
    "TaskOutcomeListener",
    "TaskSpawner",
    "TaskStatus",
    "TaskStore",
    "TextDelta",
    "ToolCall",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ToolCallRequested",
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
    "build_skill_registry",
    "create_engine",
    "create_session_factory",
    "init_db",
    "utc_now",
]
