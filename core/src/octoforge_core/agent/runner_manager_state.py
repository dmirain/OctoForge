"""Configuration and mutable state of the conversation manager."""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.api import (
    ClaimRepository,
    DialogRepository,
    ExchangeRepository,
    MessageRepository,
)
from octoforge_core.tasks.store import TaskStore

from .runner_api import DialogSurface, RunnerConfig
from .runner_constants import CLAIM_HEARTBEAT_SECONDS, CLAIM_STALE_AFTER_SECONDS

if TYPE_CHECKING:
    from .runner import ConversationRunner


@dataclass(slots=True)
class ManagerStores:
    dialogs: DialogRepository
    messages: MessageRepository
    tasks: TaskStore
    exchanges: ExchangeRepository
    claims: ClaimRepository
    uow: UnitOfWork = field(default_factory=lambda: UnitOfWork(None))


@dataclass(frozen=True, slots=True)
class OwnershipConfig:
    node_id: str
    heartbeat_seconds: float = CLAIM_HEARTBEAT_SECONDS
    stale_after_seconds: float = CLAIM_STALE_AFTER_SECONDS


@dataclass(slots=True)
class ManagerState:
    config: RunnerConfig
    stores: ManagerStores
    ownership: OwnershipConfig
    heartbeat_task: asyncio.Task[None] | None = None
    surface: DialogSurface | None = None
    runners: dict[str, "ConversationRunner"] = field(default_factory=dict)
    builds: dict[tuple[str, str], asyncio.Task["ConversationRunner"]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
