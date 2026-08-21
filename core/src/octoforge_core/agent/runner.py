"""Stable public facade for dialog actors and their manager."""

from .runner_api import (
    ConversationEvent,
    DialogSubmission,
    DialogSurface,
    RunnerConfig,
    SubscriberQueue,
    TaskOutcomeListener,
)
from .runner_commands import Flush as _Flush
from .runner_commands import ProcessTerminated as _ProcessTerminated
from .runner_commands import Unseen as _Unseen
from .runner_constants import (
    BACKGROUND_TASK_PROMPT,
    CLAIM_HEARTBEAT_SECONDS,
    CLAIM_STALE_AFTER_SECONDS,
    MATERIAL_DIGEST_CHARS,
    MATERIAL_QUIET_SECONDS,
    MATERIAL_TITLE_ANONYMOUS,
    MATERIAL_TITLE_TEMPLATE,
    NUDGE_AFTER_SECONDS,
    NUDGE_TEMPLATE,
    RESTART_LIMIT_ERROR,
    STREAM_CLOSED,
    SUBMIT_FAILED_ERROR,
    SUBSCRIBER_QUEUE_SIZE,
)
from .runner_facade import ConversationRunner
from .runner_manager import ConversationManager
from .runner_manager_state import ManagerStores, OwnershipConfig

__all__ = [
    "BACKGROUND_TASK_PROMPT",
    "CLAIM_HEARTBEAT_SECONDS",
    "CLAIM_STALE_AFTER_SECONDS",
    "MATERIAL_DIGEST_CHARS",
    "MATERIAL_QUIET_SECONDS",
    "MATERIAL_TITLE_ANONYMOUS",
    "MATERIAL_TITLE_TEMPLATE",
    "NUDGE_AFTER_SECONDS",
    "NUDGE_TEMPLATE",
    "RESTART_LIMIT_ERROR",
    "STREAM_CLOSED",
    "SUBMIT_FAILED_ERROR",
    "SUBSCRIBER_QUEUE_SIZE",
    "ConversationEvent",
    "ConversationManager",
    "ConversationRunner",
    "DialogSubmission",
    "DialogSurface",
    "ManagerStores",
    "OwnershipConfig",
    "RunnerConfig",
    "SubscriberQueue",
    "TaskOutcomeListener",
    "_Flush",
    "_ProcessTerminated",
    "_Unseen",
]
