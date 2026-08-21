"""State containers shared by the dialog actor's internal services."""

import asyncio
from collections import deque
from dataclasses import dataclass, field

from octoforge_core.db.unit_of_work import UnitOfWork
from octoforge_core.dialogs.api import (
    ClaimRepository,
    DialogClaim,
    ExchangeRepository,
    MessageRepository,
)
from octoforge_core.domain import ChatMessage, Dialog
from octoforge_core.tasks.store import TaskStore
from octoforge_core.tools.base import TaskDeleter, TaskSpawner

from .runner_api import RunnerConfig, SubscriberQueue
from .runner_commands import Command, Delivery
from .runner_process import Process


@dataclass(slots=True)
class RunnerStores:
    messages: MessageRepository
    tasks: TaskStore
    exchanges: ExchangeRepository
    claims: ClaimRepository
    uow: UnitOfWork


@dataclass(frozen=True, slots=True)
class RunnerSeed:
    dialog: Dialog
    history: list[ChatMessage]
    claim: DialogClaim


@dataclass(slots=True)
class RunnerRuntime:
    """Mutable state owned by one actor and touched only on its event loop."""

    narrative: list[ChatMessage]
    processes: dict[str, Process] = field(default_factory=dict)
    tariff_notes: dict[str, str] = field(default_factory=dict)
    pending_deliveries: deque[Delivery] = field(default_factory=deque)
    spawn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    assemble_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inbox: asyncio.Queue[Command] = field(default_factory=asyncio.Queue)
    subscribers: set[SubscriberQueue] = field(default_factory=set)
    actor_task: asyncio.Task[None] | None = None
    spawner: TaskSpawner | None = None
    deleter: TaskDeleter | None = None
    stood_down: bool = False
    preempted: bool = False
    cancel_epoch: int = 0
    seq: int = 0
    dropped_events: int = 0


@dataclass(frozen=True, slots=True)
class RunnerParts:
    seed: RunnerSeed
    config: RunnerConfig
    stores: RunnerStores
