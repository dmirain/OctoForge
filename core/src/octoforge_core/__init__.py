"""OctoForge agent core library."""

from octoforge_core.agent.control import LoopControl
from octoforge_core.agent.events import (
    AssistantMessage,
    Cancelled,
    Failed,
    Finished,
    IterationStarted,
    LoopEvent,
    TextDelta,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallRequested,
)
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.runner import ConversationEvent, ConversationManager, ConversationRunner
from octoforge_core.config import EmbeddingConfig, LLMConfig
from octoforge_core.db.engine import create_engine, create_session_factory, init_db
from octoforge_core.db.errors import DialogNotFoundError
from octoforge_core.db.repositories import DialogRepository, MessageRepository, SqlAlchemyTaskStore
from octoforge_core.domain import ChatMessage, Dialog, MessageRole, ToolCall
from octoforge_core.errors import LLMResponseError
from octoforge_core.ports import LLMClient, TaskStore
from octoforge_core.skills.base import Skill, SkillContext, SkillOrigin, SkillSpec
from octoforge_core.skills.errors import (
    DuplicateSkillError,
    SkillArgumentsError,
    SkillNotFoundError,
)
from octoforge_core.skills.registry import SkillRegistry
from octoforge_core.tasks.models import Task, TaskKind, TaskStatus
from octoforge_core.time import utc_now

__all__ = [
    "AgentLoop",
    "AssistantMessage",
    "Cancelled",
    "ChatMessage",
    "ConversationEvent",
    "ConversationManager",
    "ConversationRunner",
    "Dialog",
    "DialogNotFoundError",
    "DialogRepository",
    "DuplicateSkillError",
    "EmbeddingConfig",
    "Failed",
    "Finished",
    "IterationStarted",
    "LLMClient",
    "LLMConfig",
    "LLMResponseError",
    "LoopControl",
    "LoopEvent",
    "MessageRepository",
    "MessageRole",
    "Skill",
    "SkillArgumentsError",
    "SkillContext",
    "SkillNotFoundError",
    "SkillOrigin",
    "SkillRegistry",
    "SkillSpec",
    "SqlAlchemyTaskStore",
    "Task",
    "TaskKind",
    "TaskStatus",
    "TaskStore",
    "TextDelta",
    "ToolCall",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ToolCallRequested",
    "create_engine",
    "create_session_factory",
    "init_db",
    "utc_now",
]
