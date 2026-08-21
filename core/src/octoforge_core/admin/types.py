"""Conversation and work values returned by the admin read model."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from octoforge_core.dialogs.api import ExchangeStatus
from octoforge_core.tasks.api import TaskKind, TaskStatus

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class DialogOverview:
    id: str
    user_id: str
    channel: str
    created_at: datetime
    user_message_count: int
    agent_message_count: int
    task_count: int
    last_message_at: datetime | None
    last_user_message_at: datetime | None
    user_messages_24h: int


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: str
    dialog_id: str
    seq: int
    role: str
    content: str
    task_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskOverview:
    id: str
    dialog_id: str
    user_id: str
    channel: str
    kind: TaskKind
    title: str
    status: TaskStatus
    input: dict[str, Any]
    result: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None
    delivered_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExchangeOverview:
    id: str
    dialog_id: str
    user_id: str
    channel: str
    status: ExchangeStatus
    title: str
    pending_question: str | None
    created_at: datetime
    updated_at: datetime
