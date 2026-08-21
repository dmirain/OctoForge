"""Public types of the per-dialog actor."""

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from octoforge_core.agent.events import LoopEvent
from octoforge_core.agent.loop import AgentLoop
from octoforge_core.agent.prompts import PromptProvider
from octoforge_core.agent.router import MessageRouter
from octoforge_core.context.api import ContextCompactor
from octoforge_core.domain import MessageSource
from octoforge_core.tariffs.api import LimitGate
from octoforge_core.tasks.api import Task, TaskStatus
from octoforge_core.tools.responses import TaskScopedResponses
from octoforge_core.vision.api import ImageResolver, VisionClient

from .runner_constants import MATERIAL_QUIET_SECONDS

if TYPE_CHECKING:
    from .runner_facade import ConversationRunner


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """A loop event wrapped with its dialog, sequence and obligation."""

    dialog_id: str
    seq: int
    payload: LoopEvent
    exchange_id: str | None = None


SubscriberQueue = asyncio.Queue[ConversationEvent | None]


@dataclass(frozen=True, slots=True)
class DialogSubmission:
    """One user message entering a dialog actor."""

    content: str
    client_message_id: str | None = None
    reply_to_exchange_id: str | None = None
    source: MessageSource | None = None


class DialogSurface(Protocol):
    """A transport rendering the events of one dialog."""

    async def attach(self, runner: "ConversationRunner") -> None: ...

    async def detach(self, runner: "ConversationRunner") -> None: ...


class TaskOutcomeListener(Protocol):
    """Reports the terminal status of a task-backed process."""

    async def report_outcome(self, task: Task, status: TaskStatus) -> None: ...


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    """Behavior and collaborators shared by all runners of one manager."""

    loop: AgentLoop
    prompts: PromptProvider
    router: MessageRouter
    max_processes: int
    compactor: ContextCompactor
    task_outcome_listener: TaskOutcomeListener | None = None
    limits: LimitGate | None = None
    material_quiet_seconds: float = MATERIAL_QUIET_SECONDS
    vision: VisionClient | None = None
    image_resolver: ImageResolver | None = None
    response_memory: TaskScopedResponses | None = None
